import json, csv, io, traceback
import jwt, urllib.request
from http.server import BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs
from _helpers import (get_db, get_token_from_header, json_response, error_response,
                      cors_headers, build_campaign_filter)

def fetch_csv(url: str) -> list[dict]:
    """Baixa CSV do Google Sheets (publicado como CSV) e retorna lista de dicts."""
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=15) as resp:
        text = resp.read().decode("utf-8-sig")
    reader = csv.DictReader(io.StringIO(text))
    return [row for row in reader]

def normalize_row(row: dict) -> dict | None:
    """
    Normaliza uma linha da planilha para o mesmo formato do BigQuery.
    Retorna None se a linha for inválida.
    """
    # Mapeamento de nomes alternativos de colunas
    def get(keys):
        for k in keys:
            for col in row:
                if col.strip().lower() == k.lower():
                    return row[col].strip()
        return ""

    date         = get(["date", "data"])
    platform     = get(["platform", "plataforma"])
    campaign     = get(["CAMPAIGN_NAME", "campaign_name", "campanha"])
    ad_name      = get(["AD_NAME", "ad_name", "anuncio"])
    influenciador= get(["INFLUENCIADOR", "influenciador", "influencer"])
    impressions  = get(["IMPRESSIONS", "impressions", "impressões"])
    clicks       = get(["CLICKS", "clicks", "cliques"])
    clicks_link  = get(["CLICKS_LINK", "clicks_link"])
    thruplay     = get(["THRUPLAY", "thruplay"])
    views100     = get(["VIEWS100", "views100"])

    # Ignora linhas sem data ou impressões
    if not date or not campaign:
        return None

    def to_int(v):
        try:
            v = str(v).replace(".", "").replace(",", "").strip()
            return int(float(v)) if v else 0
        except:
            return 0

    # Normaliza data para YYYY-MM-DD
    if "/" in date:
        parts = date.split("/")
        if len(parts) == 3:
            d, m, y = parts
            if len(y) == 2:
                y = "20" + y
            date = f"{y}-{m.zfill(2)}-{d.zfill(2)}"

    return {
        "date":                   date,
        "platform":               platform or "Kwai",
        "CAMPAIGN_NAME":          campaign,
        "AD_NAME":                ad_name,
        "INFLUENCIADOR":          influenciador,
        "IMPRESSIONS":            to_int(impressions),
        "CLICKS":                 to_int(clicks),
        "CLICKS_LINK":            to_int(clicks_link) if clicks_link else to_int(clicks),
        "THRUPLAY":               to_int(thruplay),
        "VIEWS25":                0,
        "VIEWS50":                0,
        "VIEWS75":                0,
        "VIEWS100":               to_int(views100),
        "total_comments":         0,
        "total_reacoes":          0,
        "total_salvamentos":      0,
        "total_compartilhamento": 0,
    }

def filter_rows(rows, camp_filter_user, start, end):
    """Filtra por data e campanha (palavra-chave do usuário)."""
    result = []
    for r in rows:
        if not r: continue
        if r["date"] < start or r["date"] > end:
            continue
        camp = (r["CAMPAIGN_NAME"] or "").upper()
        # Usa a mesma lógica de filtro do build_campaign_filter
        # camp_filter_user é o dict do user com campaigns e exclude
        if camp_filter_user["role"] != "admin":
            kws = [k.strip().upper() for k in camp_filter_user.get("campaigns", []) if k.strip()]
            excl = [k.strip().upper() for k in camp_filter_user.get("exclude", []) if k.strip()]
            if kws:
                matched = False
                for kw in kws:
                    if "+" in kw:
                        parts = [p.strip() for p in kw.split("+") if p.strip()]
                        if all(p in camp for p in parts):
                            matched = True
                            break
                    elif kw in camp:
                        matched = True
                        break
                if not matched:
                    continue
            if excl and any(e in camp for e in excl):
                continue
        result.append(r)
    return result

def aggregate_kpi(rows):
    total = {
        "impressions": 0, "clicks": 0, "clicks_link": 0,
        "thruplay": 0, "views6": 0, "views25": 0, "views50": 0,
        "views75": 0, "views100": 0, "comments": 0, "reactions": 0,
        "saves": 0, "shares": 0
    }
    for r in rows:
        total["impressions"]  += r["IMPRESSIONS"]
        total["clicks"]       += r["CLICKS"]
        total["clicks_link"]  += r["CLICKS_LINK"]
        total["thruplay"]     += r["THRUPLAY"]
        total["views100"]     += r["VIEWS100"]
    imp = total["impressions"]
    total["ctr"]  = (total["clicks_link"] / imp * 100) if imp else 0
    total["vtr"]  = (total["thruplay"] / imp * 100) if imp else 0
    return total

def aggregate_timeseries(rows):
    by_date = {}
    for r in rows:
        d = r["date"]
        if d not in by_date:
            by_date[d] = {"date": d, "impressions": 0, "clicks": 0, "thruplay": 0,
                          "views25": 0, "views50": 0, "views75": 0, "views100": 0,
                          "comments": 0, "reactions": 0, "saves": 0, "shares": 0}
        by_date[d]["impressions"] += r["IMPRESSIONS"]
        by_date[d]["clicks"]      += r["CLICKS_LINK"]
        by_date[d]["thruplay"]    += r["THRUPLAY"]
        by_date[d]["views100"]    += r["VIEWS100"]
    result = sorted(by_date.values(), key=lambda x: x["date"])
    for row in result:
        imp = row["impressions"]
        row["ctr"] = (row["clicks"] / imp * 100) if imp else 0
        row["vtr"] = (row["thruplay"] / imp * 100) if imp else 0
    return result

def aggregate_by_campaign(rows):
    by_camp = {}
    for r in rows:
        key = (r["platform"], r["CAMPAIGN_NAME"])
        if key not in by_camp:
            by_camp[key] = {"platform": r["platform"], "CAMPAIGN_NAME": r["CAMPAIGN_NAME"],
                            "impressions": 0, "clicks": 0, "clicks_link": 0, "thruplay": 0,
                            "views25": 0, "views50": 0, "views75": 0, "views100": 0}
        by_camp[key]["impressions"]  += r["IMPRESSIONS"]
        by_camp[key]["clicks"]       += r["CLICKS"]
        by_camp[key]["clicks_link"]  += r["CLICKS_LINK"]
        by_camp[key]["thruplay"]     += r["THRUPLAY"]
        by_camp[key]["views100"]     += r["VIEWS100"]
    result = sorted(by_camp.values(), key=lambda x: -x["impressions"])
    for row in result:
        imp = row["impressions"]
        row["ctr"] = (row["clicks_link"] / imp * 100) if imp else 0
        row["vtr"] = (row["thruplay"] / imp * 100) if imp else 0
    return result

def aggregate_by_influencer(rows):
    by_infl = {}
    for r in rows:
        infl = (r["INFLUENCIADOR"] or r["AD_NAME"] or "Sem Influenciador").strip()
        # Remove sufixos de mercado
        import re
        infl = re.sub(r'\s*-\s*(BR|US|PT|MX|AR|CL|CO)\s*(C\d+)?\s*$', '', infl)
        key = (infl, r["platform"], r["CAMPAIGN_NAME"])
        if key not in by_infl:
            by_infl[key] = {"influenciador": infl, "platform": r["platform"],
                            "CAMPAIGN_NAME": r["CAMPAIGN_NAME"],
                            "impressions": 0, "clicks_link": 0, "clicks": 0,
                            "thruplay": 0, "views100": 0, "comments": 0,
                            "reactions": 0, "saves": 0, "shares": 0}
        by_infl[key]["impressions"]  += r["IMPRESSIONS"]
        by_infl[key]["clicks_link"]  += r["CLICKS_LINK"]
        by_infl[key]["clicks"]       += r["CLICKS"]
        by_infl[key]["thruplay"]     += r["THRUPLAY"]
        by_infl[key]["views100"]     += r["VIEWS100"]
    result = sorted(by_infl.values(), key=lambda x: -x["impressions"])
    for row in result:
        imp = row["impressions"]
        row["ctr_link"]  = (row["clicks_link"] / imp * 100) if imp else 0
        row["ctr_click"] = (row["clicks"] / imp * 100) if imp else 0
        row["vtr"]       = (row["thruplay"] / imp * 100) if imp else 0
    return result

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
            user = get_token_from_header(self.headers)
            parsed = urlparse(self.path)
            params = parse_qs(parsed.query)
            start  = params.get("start_date", [""])[0]
            end    = params.get("end_date",   [""])[0]
            type_  = params.get("type",       ["kpi"])[0]

            if not start or not end:
                return self._send(error_response("start_date e end_date obrigatórios."))

            # Busca URL da planilha da config
            conn = get_db(); cur = conn.cursor()
            cur.execute("SELECT value FROM app_config WHERE key='sheets_data_url'")
            row = cur.fetchone()
            cur.close(); conn.close()

            if not row or not row[0]:
                return self._send(error_response("URL da planilha não configurada.", 404))

            url = row[0]
            raw_rows = fetch_csv(url)
            norm_rows = [normalize_row(r) for r in raw_rows]
            filtered  = filter_rows([r for r in norm_rows if r], user, start, end)

            if type_ == "kpi":
                self._send(json_response(aggregate_kpi(filtered)))
            elif type_ == "timeseries":
                self._send(json_response({"rows": aggregate_timeseries(filtered)}))
            elif type_ == "by_campaign":
                self._send(json_response({"rows": aggregate_by_campaign(filtered)}))
            elif type_ == "by_influencer":
                self._send(json_response({"rows": aggregate_by_influencer(filtered)}))
            else:
                self._send(error_response("Tipo inválido."))

        except (PermissionError, jwt.ExpiredSignatureError) as e:
            self._send(error_response(str(e), 401))
        except Exception as e:
            tb = traceback.format_exc()
            self._send(error_response(f"ERRO: {str(e)} | {tb}", 500))

app = handler
