import json, os, traceback
import jwt
from http.server import BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs
from _helpers import (get_bq, build_campaign_filter, get_token_from_header,
                      json_response, error_response, cors_headers, BQ_TABLE)

BQ_TABLE_SAFE = f"`{BQ_TABLE}`"

def bq_rows(query):
    bq = get_bq()
    return [dict(r) for r in bq.query(query).result()]

def get_kpi(camp_filter, start, end):
    q = f"""
    SELECT
        SUM(COALESCE(IMPRESSIONS, 0))                   AS impressions,
        SUM(COALESCE(CLICKS, 0))                        AS clicks,
        SUM(COALESCE(CLICKS_LINK, 0))                   AS clicks_link,
        SUM(COALESCE(THRUPLAY, 0))                      AS thruplay,
        SUM(COALESCE(VIEWS6,  0))                       AS views6,
        SUM(COALESCE(VIEWS25, 0))                       AS views25,
        SUM(COALESCE(VIEWS50, 0))                       AS views50,
        SUM(COALESCE(VIEWS75, 0))                       AS views75,
        SUM(COALESCE(VIEWS100,0))                       AS views100,
        SUM(COALESCE(total_comments, 0))                AS comments,
        SUM(COALESCE(total_reacoes,  0))                AS reactions,
        SUM(COALESCE(total_salvamentos, 0))             AS saves,
        SUM(COALESCE(total_compartilhamento, 0))        AS shares,
        SAFE_DIVIDE(SUM(COALESCE(CLICKS,0)),NULLIF(SUM(COALESCE(IMPRESSIONS,0)),0))*100 AS ctr,
        SAFE_DIVIDE(SUM(COALESCE(THRUPLAY,0)),NULLIF(SUM(COALESCE(IMPRESSIONS,0)),0))*100 AS vtr
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
        SUM(COALESCE(total_comments,0))         AS comments,
        SUM(COALESCE(total_reacoes,0))          AS reactions,
        SUM(COALESCE(total_salvamentos,0))      AS saves,
        SUM(COALESCE(total_compartilhamento,0)) AS shares,
        SAFE_DIVIDE(SUM(COALESCE(CLICKS,0)),NULLIF(SUM(COALESCE(IMPRESSIONS,0)),0))*100 AS ctr,
        SAFE_DIVIDE(SUM(COALESCE(THRUPLAY,0)),NULLIF(SUM(COALESCE(IMPRESSIONS,0)),0))*100 AS vtr
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
        SAFE_DIVIDE(SUM(COALESCE(CLICKS,0)),NULLIF(SUM(COALESCE(IMPRESSIONS,0)),0))*100 AS ctr,
        SAFE_DIVIDE(SUM(COALESCE(THRUPLAY,0)),NULLIF(SUM(COALESCE(IMPRESSIONS,0)),0))*100 AS vtr
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
        SUM(COALESCE(IMPRESSIONS,0))   AS impressions,
        SUM(COALESCE(CLICKS_LINK,0))   AS clicks_link,
        SUM(COALESCE(CLICKS,0))        AS clicks,
        SUM(COALESCE(THRUPLAY,0))      AS thruplay,
        SUM(COALESCE(VIEWS25,0))       AS views25,
        SUM(COALESCE(VIEWS50,0))       AS views50,
        SUM(COALESCE(VIEWS75,0))       AS views75,
        SUM(COALESCE(VIEWS100,0))      AS views100,
        SUM(COALESCE(total_comments,0))         AS comments,
        SUM(COALESCE(total_reacoes,0))          AS reactions,
        SUM(COALESCE(total_salvamentos,0))      AS saves,
        SUM(COALESCE(total_compartilhamento,0)) AS shares,
        SAFE_DIVIDE(SUM(COALESCE(CLICKS_LINK,0)),NULLIF(SUM(COALESCE(IMPRESSIONS,0)),0))*100 AS ctr_link,
        SAFE_DIVIDE(SUM(COALESCE(CLICKS,0)),NULLIF(SUM(COALESCE(IMPRESSIONS,0)),0))*100 AS ctr_click,
        SAFE_DIVIDE(SUM(COALESCE(THRUPLAY,0)),NULLIF(SUM(COALESCE(IMPRESSIONS,0)),0))*100 AS vtr
    FROM {BQ_TABLE_SAFE}
    WHERE date BETWEEN '{start}' AND '{end}' AND {camp_filter}
    GROUP BY influenciador, platform, CAMPAIGN_NAME ORDER BY impressions DESC
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

            if type_ == "kpi":
                result = get_kpi(camp_filter, start, end)
            elif type_ == "timeseries":
                result = {"rows": get_timeseries(camp_filter, start, end)}
            elif type_ == "by_campaign":
                result = {"rows": get_by_campaign(camp_filter, start, end)}
            elif type_ == "by_influencer":
                result = {"rows": get_by_influencer(camp_filter, start, end)}
            else:
                return self._send(error_response("Tipo inválido."))

            self._send(json_response(result))

        except (PermissionError, jwt.ExpiredSignatureError) as e:
            self._send(error_response(str(e), 401))
        except Exception as e:
            tb = traceback.format_exc()
            self._send(error_response(f"ERRO: {str(e)} | TRACEBACK: {tb}", 500))

app = handler
