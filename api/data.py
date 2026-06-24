"""
data.py atualizado — mescla BigQuery + Google Sheets (planilha de mídia paga extra).
Se sheets_data_url estiver configurado no banco, os dados são somados aos do BigQuery.
"""
import json, os, traceback
import jwt
from http.server import BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs
from _helpers import (get_bq, get_db, build_campaign_filter, get_token_from_header,
                      json_response, error_response, cors_headers, BQ_TABLE)
from sheets_data import (fetch_csv, normalize_row, filter_rows,
                         aggregate_kpi, aggregate_timeseries,
                         aggregate_by_campaign, aggregate_by_influencer)

BQ_TABLE_SAFE = f"`{BQ_TABLE}`"

def bq_rows(query):
    bq = get_bq()
    return [dict(r) for r in bq.query(query).result()]

def get_sheets_rows(user, start, end):
    """Retorna linhas da planilha configurada, ou [] se não houver."""
    try:
        conn = get_db(); cur = conn.cursor()
        cur.execute("SELECT value FROM app_config WHERE key='sheets_data_url'")
        row = cur.fetchone()
        cur.close(); conn.close()
        if not row or not row[0]:
            return []
        raw = fetch_csv(row[0])
        norm = [normalize_row(r) for r in raw]
        return filter_rows([r for r in norm if r], user, start, end)
    except Exception as e:
        print(f"[sheets] erro: {e}")
        return []

def merge_kpi(bq_kpi, sheet_rows):
    if not sheet_rows:
        return bq_kpi
    s = aggregate_kpi(sheet_rows)
    merged = dict(bq_kpi)
    for k in ["impressions","clicks","clicks_link","thruplay","views100",
              "comments","reactions","saves","shares","views6","views25","views50","views75"]:
        merged[k] = (merged.get(k) or 0) + (s.get(k) or 0)
    imp = merged.get("impressions") or 0
    merged["ctr"] = (merged["clicks_link"] / imp * 100) if imp else 0
    merged["vtr"] = (merged["thruplay"]    / imp * 100) if imp else 0
    return merged

def merge_timeseries(bq_rows_list, sheet_rows):
    if not sheet_rows:
        return bq_rows_list
    s_map = {r["date"]: r for r in aggregate_timeseries(sheet_rows)}
    b_map = {r["date"]: r for r in bq_rows_list}
    all_dates = sorted(set(list(s_map.keys()) + list(b_map.keys())))
    result = []
    for d in all_dates:
        bq = b_map.get(d, {})
        sh = s_map.get(d, {})
        imp = (bq.get("impressions") or 0) + (sh.get("impressions") or 0)
        clk = (bq.get("clicks") or 0) + (sh.get("clicks") or 0)
        tp  = (bq.get("thruplay") or 0) + (sh.get("thruplay") or 0)
        result.append({
            "date": d,
            "impressions": imp,
            "clicks": clk,
            "thruplay": tp,
            "views25": (bq.get("views25") or 0) + (sh.get("views25") or 0),
            "views50": (bq.get("views50") or 0) + (sh.get("views50") or 0),
            "views75": (bq.get("views75") or 0) + (sh.get("views75") or 0),
            "views100": (bq.get("views100") or 0) + (sh.get("views100") or 0),
            "comments": (bq.get("comments") or 0),
            "reactions": (bq.get("reactions") or 0),
            "saves": (bq.get("saves") or 0),
            "shares": (bq.get("shares") or 0),
            "ctr": (clk / imp * 100) if imp else 0,
            "vtr": (tp  / imp * 100) if imp else 0,
        })
    return result

def merge_by_campaign(bq_list, sheet_rows):
    if not sheet_rows:
        return bq_list
    s_list = aggregate_by_campaign(sheet_rows)
    combined = {(r["platform"], r["CAMPAIGN_NAME"]): dict(r) for r in bq_list}
    for s in s_list:
        key = (s["platform"], s["CAMPAIGN_NAME"])
        if key in combined:
            for f in ["impressions","clicks","clicks_link","thruplay","views100"]:
                combined[key][f] = (combined[key].get(f) or 0) + (s.get(f) or 0)
        else:
            combined[key] = dict(s)
    result = sorted(combined.values(), key=lambda x: -(x.get("impressions") or 0))
    for row in result:
        imp = row.get("impressions") or 0
        row["ctr"] = (row.get("clicks_link", 0) / imp * 100) if imp else 0
        row["vtr"] = (row.get("thruplay", 0)    / imp * 100) if imp else 0
    return result

def merge_by_influencer(bq_list, sheet_rows):
    if not sheet_rows:
        return bq_list
    s_list = aggregate_by_influencer(sheet_rows)
    combined = {(r["influenciador"], r["platform"], r["CAMPAIGN_NAME"]): dict(r) for r in bq_list}
    for s in s_list:
        key = (s["influenciador"], s["platform"], s["CAMPAIGN_NAME"])
        if key in combined:
            for f in ["impressions","clicks_link","clicks","thruplay","views100"]:
                combined[key][f] = (combined[key].get(f) or 0) + (s.get(f) or 0)
        else:
            combined[key] = dict(s)
    result = sorted(combined.values(), key=lambda x: -(x.get("impressions") or 0))
    for row in result:
        imp = row.get("impressions") or 0
        row["ctr_link"]  = (row.get("clicks_link", 0) / imp * 100) if imp else 0
        row["ctr_click"] = (row.get("clicks", 0)      / imp * 100) if imp else 0
        row["vtr"]       = (row.get("thruplay", 0)    / imp * 100) if imp else 0
    return result

# ── BQ QUERIES ────────────────────────────────────────────────

def get_kpi(camp_filter, start, end):
    q = f"""
    SELECT
        SUM(COALESCE(IMPRESSIONS, 0))            AS impressions,
        SUM(COALESCE(CLICKS, 0))                 AS clicks,
        SUM(COALESCE(CLICKS_LINK, 0))            AS clicks_link,
        SUM(COALESCE(THRUPLAY, 0))               AS thruplay,
        SUM(COALESCE(VIEWS6,  0))                AS views6,
        SUM(COALESCE(VIEWS25, 0))                AS views25,
        SUM(COALESCE(VIEWS50, 0))                AS views50,
        SUM(COALESCE(VIEWS75, 0))                AS views75,
        SUM(COALESCE(VIEWS100,0))                AS views100,
        SUM(COALESCE(total_comments, 0))         AS comments,
        SUM(COALESCE(total_reacoes,  0))         AS reactions,
        SUM(COALESCE(total_salvamentos, 0))      AS saves,
        SUM(COALESCE(total_compartilhamento, 0)) AS shares,
        SAFE_DIVIDE(SUM(COALESCE(CLICKS,0)),
            NULLIF(SUM(COALESCE(IMPRESSIONS,0)),0)) * 100 AS ctr,
        SAFE_DIVIDE(SUM(COALESCE(THRUPLAY,0)),
            NULLIF(SUM(COALESCE(IMPRESSIONS,0)),0)) * 100 AS vtr
    FROM {BQ_TABLE_SAFE}
    WHERE date BETWEEN '{start}' AND '{end}' AND {camp_filter}
    """
    rows = bq_rows(q)
    return rows[0] if rows else {}

def get_timeseries(camp_filter, start, end):
    q = f"""
    SELECT CAST(date AS STRING) AS date,
        SUM(COALESCE(IMPRESSIONS,0)) AS impressions,
        SUM(COALESCE(CLICKS,0))      AS clicks,
        SUM(COALESCE(THRUPLAY,0))    AS thruplay,
        SUM(COALESCE(VIEWS25,0))     AS views25,
        SUM(COALESCE(VIEWS50,0))     AS views50,
        SUM(COALESCE(VIEWS75,0))     AS views75,
        SUM(COALESCE(VIEWS100,0))    AS views100,
        SUM(COALESCE(total_comments,0))          AS comments,
        SUM(COALESCE(total_reacoes,0))           AS reactions,
        SUM(COALESCE(total_salvamentos,0))       AS saves,
        SUM(COALESCE(total_compartilhamento,0))  AS shares,
        SAFE_DIVIDE(SUM(COALESCE(CLICKS,0)),
            NULLIF(SUM(COALESCE(IMPRESSIONS,0)),0)) * 100 AS ctr,
        SAFE_DIVIDE(SUM(COALESCE(THRUPLAY,0)),
            NULLIF(SUM(COALESCE(IMPRESSIONS,0)),0)) * 100 AS vtr
    FROM {BQ_TABLE_SAFE}
    WHERE date BETWEEN '{start}' AND '{end}' AND {camp_filter}
    GROUP BY date ORDER BY date ASC
    """
    return bq_rows(q)

def get_by_campaign(camp_filter, start, end):
    q = f"""
    SELECT platform, CAMPAIGN_NAME,
        SUM(COALESCE(IMPRESSIONS,0))   AS impressions,
        SUM(COALESCE(CLICKS,0))        AS clicks,
        SUM(COALESCE(CLICKS_LINK,0))   AS clicks_link,
        SUM(COALESCE(THRUPLAY,0))      AS thruplay,
        SUM(COALESCE(VIEWS25,0))       AS views25,
        SUM(COALESCE(VIEWS50,0))       AS views50,
        SUM(COALESCE(VIEWS75,0))       AS views75,
        SUM(COALESCE(VIEWS100,0))      AS views100,
        SAFE_DIVIDE(SUM(COALESCE(CLICKS,0)),
            NULLIF(SUM(COALESCE(IMPRESSIONS,0)),0)) * 100 AS ctr,
        SAFE_DIVIDE(SUM(COALESCE(THRUPLAY,0)),
            NULLIF(SUM(COALESCE(IMPRESSIONS,0)),0)) * 100 AS vtr
    FROM {BQ_TABLE_SAFE}
    WHERE date BETWEEN '{start}' AND '{end}' AND {camp_filter}
    GROUP BY platform, CAMPAIGN_NAME ORDER BY impressions DESC
    """
    return bq_rows(q)

def get_by_influencer(camp_filter, start, end):
    q = f"""
    SELECT
        TRIM(REGEXP_REPLACE(
            COALESCE(NULLIF(TRIM(INFLUENCIADOR),''),NULLIF(TRIM(AD_NAME),''),'Sem Influenciador'),
            r'\\s*-\\s*(BR|US|PT|MX|AR|CL|CO)\\s*(C\\d+)?\\s*$', ''
        )) AS influenciador,
        platform, CAMPAIGN_NAME,
        SUM(COALESCE(IMPRESSIONS,0))             AS impressions,
        SUM(COALESCE(CLICKS_LINK,0))             AS clicks_link,
        SUM(COALESCE(CLICKS,0))                  AS clicks,
        SUM(COALESCE(THRUPLAY,0))                AS thruplay,
        SUM(COALESCE(VIEWS25,0))                 AS views25,
        SUM(COALESCE(VIEWS50,0))                 AS views50,
        SUM(COALESCE(VIEWS75,0))                 AS views75,
        SUM(COALESCE(VIEWS100,0))                AS views100,
        SUM(COALESCE(total_comments,0))          AS comments,
        SUM(COALESCE(total_reacoes,0))           AS reactions,
        SUM(COALESCE(total_salvamentos,0))       AS saves,
        SUM(COALESCE(total_compartilhamento,0))  AS shares,
        SAFE_DIVIDE(SUM(COALESCE(CLICKS_LINK,0)),
            NULLIF(SUM(COALESCE(IMPRESSIONS,0)),0)) * 100 AS ctr_link,
        SAFE_DIVIDE(SUM(COALESCE(CLICKS,0)),
            NULLIF(SUM(COALESCE(IMPRESSIONS,0)),0)) * 100 AS ctr_click,
        SAFE_DIVIDE(SUM(COALESCE(THRUPLAY,0)),
            NULLIF(SUM(COALESCE(IMPRESSIONS,0)),0)) * 100 AS vtr
    FROM {BQ_TABLE_SAFE}
    WHERE date BETWEEN '{start}' AND '{end}' AND {camp_filter}
    GROUP BY influenciador, platform, CAMPAIGN_NAME
    ORDER BY impressions DESC
    """
    return bq_rows(q)


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
                return self._send(error_response("Parâmetros start_date e end_date obrigatórios."))

            camp_filter = build_campaign_filter(user)

            # Dados da planilha (extra, se configurada)
            sheet_rows = get_sheets_rows(user, start, end)

            if type_ == "kpi":
                bq = get_kpi(camp_filter, start, end)
                result = merge_kpi(bq, sheet_rows)
            elif type_ == "timeseries":
                bq = get_timeseries(camp_filter, start, end)
                result = {"rows": merge_timeseries(bq, sheet_rows)}
            elif type_ == "by_campaign":
                bq = get_by_campaign(camp_filter, start, end)
                result = {"rows": merge_by_campaign(bq, sheet_rows)}
            elif type_ == "by_influencer":
                bq = get_by_influencer(camp_filter, start, end)
                result = {"rows": merge_by_influencer(bq, sheet_rows)}
            else:
                return self._send(error_response("Tipo inválido."))

            self._send(json_response(result))

        except (PermissionError, jwt.ExpiredSignatureError) as e:
            self._send(error_response(str(e), 401))
        except Exception as e:
            tb = traceback.format_exc()
            self._send(error_response(f"ERRO: {str(e)} | TRACEBACK: {tb}", 500))

app = handler
