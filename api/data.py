import json, os, csv, io, traceback, re
import jwt, urllib.request
from http.server import BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs
from _helpers import (get_bq, get_db, build_campaign_filter, get_token_from_header,
                      json_response, error_response, cors_headers, BQ_TABLE)

BQ_TABLE_SAFE = f"`{BQ_TABLE}`"

# ── PLANILHA EXTRA (URL via env var SHEETS_DATA_URL) ──────────

def _fetch_csv(url):
    req = urllib.request.Request(url.strip(), headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=15) as resp:
        text = resp.read().decode("utf-8-sig")
    reader = csv.DictReader(io.StringIO(text))
    return list(reader)

def _to_int(v):
    try:
        return int(float(str(v).replace(".", "").replace(",", "").strip()))
    except:
        return 0

def _normalize(row):
    def get(*keys):
        for k in keys:
            for col in row:
                if col.strip().lower() == k.lower():
                    v = row[col]
                    return v.strip() if isinstance(v, str) else str(v)
        return ""

    date = get("date", "data")
    camp = get("CAMPAIGN_NAME", "campaign_name", "campanha")
    if not date or not camp:
        return None

    if "/" in date:
        parts = date.split("/")
        if len(parts) == 3:
            d, m, y = parts
            if len(y) == 2:
                y = "20" + y
            date = f"{y}-{m.zfill(2)}-{d.zfill(2)}"

    clicks      = get("CLICKS", "clicks", "cliques")
    clicks_link = get("CLICKS_LINK", "clicks_link")

    return {
        "date":        date,
        "platform":    get("platform", "plataforma") or "Kwai",
        "CAMPAIGN_NAME": camp,
        "AD_NAME":     get("AD_NAME", "ad_name"),
        "INFLUENCIADOR": get("INFLUENCIADOR", "influenciador"),
        "IMPRESSIONS": _to_int(get("IMPRESSIONS", "impressions", "impressões")),
        "CLICKS":      _to_int(clicks),
        "CLICKS_LINK": _to_int(clicks_link) if clicks_link else _to_int(clicks),
        "THRUPLAY":    _to_int(get("THRUPLAY", "thruplay")),
        "VIEWS100":    _to_int(get("VIEWS100", "views100")),
    }

def _filter_sheet(rows, user, start, end):
    out = []
    for r in rows:
        if not r or r["date"] < start or r["date"] > end:
            continue
        camp = (r["CAMPAIGN_NAME"] or "").upper()
        if user["role"] != "admin":
            kws  = [k.strip().upper() for k in user.get("campaigns", []) if k.strip()]
            excl = [k.strip().upper() for k in user.get("exclude", []) if k.strip()]
            if kws:
                matched = any(
                    all(p in camp for p in kw.split("+")) if "+" in kw else kw in camp
                    for kw in kws
                )
                if not matched:
                    continue
            if excl and any(e in camp for e in excl):
                continue
        out.append(r)
    return out

def _get_sheet_rows(user, start, end):
    # URL via env var (mais simples e confiável que banco)
    url = os.environ.get("SHEETS_DATA_URL", "").strip()
    if not url:
        # Fallback: tenta buscar do banco
        try:
            conn = get_db(); cur = conn.cursor()
            cur.execute("""
                CREATE TABLE IF NOT EXISTS app_config (
                    key TEXT PRIMARY KEY, value TEXT NOT NULL
                )
            """)
            conn.commit()
            cur.execute("SELECT value FROM app_config WHERE key='sheets_data_url'")
            row = cur.fetchone()
            cur.close(); conn.close()
            if row and row[0]:
                url = row[0].strip()
        except:
            pass
    if not url:
        return []
    try:
        raw      = _fetch_csv(url)
        norm     = [_normalize(r) for r in raw]
        filtered = _filter_sheet([r for r in norm if r], user, start, end)
        return filtered
    except Exception as e:
        print(f"[sheets] erro: {e}")
        return []

# ── MERGE HELPERS ─────────────────────────────────────────────

def _sheet_kpi(rows):
    imp = clk = clkl = tp = v100 = 0
    for r in rows:
        imp  += r["IMPRESSIONS"]
        clk  += r["CLICKS"]
        clkl += r["CLICKS_LINK"]
        tp   += r["THRUPLAY"]
        v100 += r["VIEWS100"]
    return {"impressions": imp, "clicks": clk, "clicks_link": clkl,
            "thruplay": tp, "views100": v100,
            "ctr": (clkl / imp * 100) if imp else 0,
            "vtr": (tp   / imp * 100) if imp else 0}

def _merge_kpi(bq, srows):
    if not srows: return bq
    s = _sheet_kpi(srows)
    m = dict(bq)
    for k in ["impressions","clicks","clicks_link","thruplay","views100"]:
        m[k] = (m.get(k) or 0) + s.get(k, 0)
    imp = m.get("impressions") or 0
    m["ctr"] = (m["clicks_link"] / imp * 100) if imp else 0
    m["vtr"] = (m["thruplay"]    / imp * 100) if imp else 0
    return m

def _merge_timeseries(bq_list, srows):
    if not srows: return bq_list
    smap = {}
    for r in srows:
        d = r["date"]
        if d not in smap:
            smap[d] = {"date":d,"impressions":0,"clicks":0,"thruplay":0,"views100":0}
        smap[d]["impressions"] += r["IMPRESSIONS"]
        smap[d]["clicks"]      += r["CLICKS_LINK"]
        smap[d]["thruplay"]    += r["THRUPLAY"]
        smap[d]["views100"]    += r["VIEWS100"]
    bmap = {r["date"]: r for r in bq_list}
    for d, s in smap.items():
        if d in bmap:
            bmap[d]["impressions"] += s["impressions"]
            bmap[d]["clicks"]      += s["clicks"]
            bmap[d]["thruplay"]    += s["thruplay"]
            imp = bmap[d]["impressions"]
            bmap[d]["ctr"] = (bmap[d]["clicks"] / imp * 100) if imp else 0
            bmap[d]["vtr"] = (bmap[d]["thruplay"] / imp * 100) if imp else 0
        else:
            imp = s["impressions"]
            s["ctr"] = (s["clicks"] / imp * 100) if imp else 0
            s["vtr"] = (s["thruplay"] / imp * 100) if imp else 0
            bmap[d] = s
    return sorted(bmap.values(), key=lambda x: x["date"])

def _merge_by_campaign(bq_list, srows):
    if not srows: return bq_list
    combined = {(r["platform"], r["CAMPAIGN_NAME"]): dict(r) for r in bq_list}
    for r in srows:
        key = (r["platform"], r["CAMPAIGN_NAME"])
        if key not in combined:
            combined[key] = {"platform": r["platform"], "CAMPAIGN_NAME": r["CAMPAIGN_NAME"],
                             "impressions":0,"clicks":0,"clicks_link":0,"thruplay":0,"views100":0}
        for f in ["impressions","clicks","clicks_link","thruplay","views100"]:
            combined[key][f] = (combined[key].get(f) or 0) + (r.get(f.upper()) or r.get(f) or 0)
    result = sorted(combined.values(), key=lambda x: -(x.get("impressions") or 0))
    for row in result:
        imp = row.get("impressions") or 0
        row["ctr"] = (row.get("clicks_link",0) / imp * 100) if imp else 0
        row["vtr"] = (row.get("thruplay",0)    / imp * 100) if imp else 0
    return result

def _merge_by_influencer(bq_list, srows):
    if not srows: return bq_list
    combined = {(r["influenciador"], r["platform"], r["CAMPAIGN_NAME"]): dict(r) for r in bq_list}
    for r in srows:
        infl = (r["INFLUENCIADOR"] or r["AD_NAME"] or "Sem Influenciador").strip()
        infl = re.sub(r'\s*-\s*(BR|US|PT|MX|AR|CL|CO)\s*(C\d+)?\s*$', '', infl)
        key = (infl, r["platform"], r["CAMPAIGN_NAME"])
        if key not in combined:
            combined[key] = {"influenciador":infl,"platform":r["platform"],
                             "CAMPAIGN_NAME":r["CAMPAIGN_NAME"],
                             "impressions":0,"clicks_link":0,"clicks":0,"thruplay":0,"views100":0}
        combined[key]["impressions"]  += r["IMPRESSIONS"]
        combined[key]["clicks_link"]  += r["CLICKS_LINK"]
        combined[key]["clicks"]       += r["CLICKS"]
        combined[key]["thruplay"]     += r["THRUPLAY"]
        combined[key]["views100"]     += r["VIEWS100"]
    result = sorted(combined.values(), key=lambda x: -(x.get("impressions") or 0))
    for row in result:
        imp = row.get("impressions") or 0
        row["ctr_link"]  = (row.get("clicks_link",0) / imp * 100) if imp else 0
        row["ctr_click"] = (row.get("clicks",0)      / imp * 100) if imp else 0
        row["vtr"]       = (row.get("thruplay",0)    / imp * 100) if imp else 0
    return result

# ── BIGQUERY QUERIES ──────────────────────────────────────────

def bq_rows(query):
    return [dict(r) for r in get_bq().query(query).result()]

def get_kpi(f, s, e):
    q = f"""
    SELECT SUM(COALESCE(IMPRESSIONS,0)) AS impressions, SUM(COALESCE(CLICKS,0)) AS clicks,
        SUM(COALESCE(CLICKS_LINK,0)) AS clicks_link, SUM(COALESCE(THRUPLAY,0)) AS thruplay,
        SUM(COALESCE(VIEWS6,0)) AS views6, SUM(COALESCE(VIEWS25,0)) AS views25,
        SUM(COALESCE(VIEWS50,0)) AS views50, SUM(COALESCE(VIEWS75,0)) AS views75,
        SUM(COALESCE(VIEWS100,0)) AS views100,
        SUM(COALESCE(total_comments,0)) AS comments, SUM(COALESCE(total_reacoes,0)) AS reactions,
        SUM(COALESCE(total_salvamentos,0)) AS saves,
        SUM(COALESCE(total_compartilhamento,0)) AS shares,
        SAFE_DIVIDE(SUM(COALESCE(CLICKS,0)),NULLIF(SUM(COALESCE(IMPRESSIONS,0)),0))*100 AS ctr,
        SAFE_DIVIDE(SUM(COALESCE(THRUPLAY,0)),NULLIF(SUM(COALESCE(IMPRESSIONS,0)),0))*100 AS vtr
    FROM {BQ_TABLE_SAFE}
    WHERE date BETWEEN '{s}' AND '{e}' AND {f}"""
    rows = bq_rows(q); return rows[0] if rows else {}

def get_timeseries(f, s, e):
    q = f"""
    SELECT CAST(date AS STRING) AS date,
        SUM(COALESCE(IMPRESSIONS,0)) AS impressions, SUM(COALESCE(CLICKS,0)) AS clicks,
        SUM(COALESCE(THRUPLAY,0)) AS thruplay, SUM(COALESCE(VIEWS25,0)) AS views25,
        SUM(COALESCE(VIEWS50,0)) AS views50, SUM(COALESCE(VIEWS75,0)) AS views75,
        SUM(COALESCE(VIEWS100,0)) AS views100,
        SUM(COALESCE(total_comments,0)) AS comments, SUM(COALESCE(total_reacoes,0)) AS reactions,
        SUM(COALESCE(total_salvamentos,0)) AS saves, SUM(COALESCE(total_compartilhamento,0)) AS shares,
        SAFE_DIVIDE(SUM(COALESCE(CLICKS,0)),NULLIF(SUM(COALESCE(IMPRESSIONS,0)),0))*100 AS ctr,
        SAFE_DIVIDE(SUM(COALESCE(THRUPLAY,0)),NULLIF(SUM(COALESCE(IMPRESSIONS,0)),0))*100 AS vtr
    FROM {BQ_TABLE_SAFE}
    WHERE date BETWEEN '{s}' AND '{e}' AND {f}
    GROUP BY date ORDER BY date ASC"""
    return bq_rows(q)

def get_by_campaign(f, s, e):
    q = f"""
    SELECT platform, CAMPAIGN_NAME,
        SUM(COALESCE(IMPRESSIONS,0)) AS impressions, SUM(COALESCE(CLICKS,0)) AS clicks,
        SUM(COALESCE(CLICKS_LINK,0)) AS clicks_link, SUM(COALESCE(THRUPLAY,0)) AS thruplay,
        SUM(COALESCE(VIEWS25,0)) AS views25, SUM(COALESCE(VIEWS50,0)) AS views50,
        SUM(COALESCE(VIEWS75,0)) AS views75, SUM(COALESCE(VIEWS100,0)) AS views100,
        SAFE_DIVIDE(SUM(COALESCE(CLICKS,0)),NULLIF(SUM(COALESCE(IMPRESSIONS,0)),0))*100 AS ctr,
        SAFE_DIVIDE(SUM(COALESCE(THRUPLAY,0)),NULLIF(SUM(COALESCE(IMPRESSIONS,0)),0))*100 AS vtr
    FROM {BQ_TABLE_SAFE}
    WHERE date BETWEEN '{s}' AND '{e}' AND {f}
    GROUP BY platform, CAMPAIGN_NAME ORDER BY impressions DESC"""
    return bq_rows(q)

def get_by_influencer(f, s, e):
    q = f"""
    SELECT TRIM(REGEXP_REPLACE(
            COALESCE(NULLIF(TRIM(INFLUENCIADOR),''),NULLIF(TRIM(AD_NAME),''),'Sem Influenciador'),
            r'\\s*-\\s*(BR|US|PT|MX|AR|CL|CO)\\s*(C\\d+)?\\s*$','')) AS influenciador,
        platform, CAMPAIGN_NAME,
        SUM(COALESCE(IMPRESSIONS,0)) AS impressions, SUM(COALESCE(CLICKS_LINK,0)) AS clicks_link,
        SUM(COALESCE(CLICKS,0)) AS clicks, SUM(COALESCE(THRUPLAY,0)) AS thruplay,
        SUM(COALESCE(VIEWS25,0)) AS views25, SUM(COALESCE(VIEWS50,0)) AS views50,
        SUM(COALESCE(VIEWS75,0)) AS views75, SUM(COALESCE(VIEWS100,0)) AS views100,
        SUM(COALESCE(total_comments,0)) AS comments, SUM(COALESCE(total_reacoes,0)) AS reactions,
        SUM(COALESCE(total_salvamentos,0)) AS saves, SUM(COALESCE(total_compartilhamento,0)) AS shares,
        SAFE_DIVIDE(SUM(COALESCE(CLICKS_LINK,0)),NULLIF(SUM(COALESCE(IMPRESSIONS,0)),0))*100 AS ctr_link,
        SAFE_DIVIDE(SUM(COALESCE(CLICKS,0)),NULLIF(SUM(COALESCE(IMPRESSIONS,0)),0))*100 AS ctr_click,
        SAFE_DIVIDE(SUM(COALESCE(THRUPLAY,0)),NULLIF(SUM(COALESCE(IMPRESSIONS,0)),0))*100 AS vtr
    FROM {BQ_TABLE_SAFE}
    WHERE date BETWEEN '{s}' AND '{e}' AND {f}
    GROUP BY influenciador, platform, CAMPAIGN_NAME ORDER BY impressions DESC"""
    return bq_rows(q)

# ── HANDLER ───────────────────────────────────────────────────

class handler(BaseHTTPRequestHandler):

    def do_OPTIONS(self):
        self.send_response(200)
        for k, v in cors_headers().items(): self.send_header(k, v)
        self.end_headers()

    def _send(self, resp):
        self.send_response(resp["statusCode"])
        for k, v in resp["headers"].items(): self.send_header(k, v)
        self.end_headers()
        self.wfile.write(resp["body"].encode())

    def do_GET(self):
        try:
            user   = get_token_from_header(self.headers)
            parsed = urlparse(self.path)
            params = parse_qs(parsed.query)
            start  = params.get("start_date", [""])[0]
            end    = params.get("end_date",   [""])[0]
            type_  = params.get("type",       ["kpi"])[0]

            if not start or not end:
                return self._send(error_response("Parâmetros start_date e end_date obrigatórios."))

            camp_filter = build_campaign_filter(user)
            srows       = _get_sheet_rows(user, start, end)

            if type_ == "kpi":
                result = _merge_kpi(get_kpi(camp_filter, start, end), srows)
            elif type_ == "timeseries":
                result = {"rows": _merge_timeseries(get_timeseries(camp_filter, start, end), srows)}
            elif type_ == "by_campaign":
                result = {"rows": _merge_by_campaign(get_by_campaign(camp_filter, start, end), srows)}
            elif type_ == "by_influencer":
                result = {"rows": _merge_by_influencer(get_by_influencer(camp_filter, start, end), srows)}
            else:
                return self._send(error_response("Tipo inválido."))

            self._send(json_response(result))

        except (PermissionError, jwt.ExpiredSignatureError) as e:
            self._send(error_response(str(e), 401))
        except Exception as e:
            tb = traceback.format_exc()
            self._send(error_response(f"ERRO: {str(e)} | TRACEBACK: {tb}", 500))

app = handler
