import json
import jwt
from http.server import BaseHTTPRequestHandler
from _helpers import (get_db, init_db, get_token_from_header,
                      json_response, error_response, cors_headers)

def ensure_config_table():
    conn = get_db()
    cur  = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS app_config (
            key   TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
    """)
    conn.commit()
    cur.close(); conn.close()

def get_config():
    ensure_config_table()
    conn = get_db()
    cur  = conn.cursor()
    cur.execute("""
        SELECT key, value FROM app_config
        WHERE key IN ('camps','sheets_url','sheets_urls','access_sheets','sheets_data_url')
    """)
    rows = {r[0]: r[1] for r in cur.fetchall()}
    cur.close(); conn.close()

    camps = json.loads(rows.get('camps', 'null'))
    if not camps:
        camps = [
            {"nome": "Campanha 1", "kw": "", "click": "clicks"},
            {"nome": "Campanha 2", "kw": "", "click": "clicks"},
            {"nome": "Campanha 3", "kw": "", "click": "clicks"},
        ]
    sheets_urls   = json.loads(rows.get('sheets_urls', '{}')) if rows.get('sheets_urls') else {}
    access_sheets = json.loads(rows.get('access_sheets', '[]')) if rows.get('access_sheets') else []
    sheets_data_url = rows.get('sheets_data_url', '')

    return {
        "camps":           camps,
        "sheets_url":      rows.get('sheets_url', ''),
        "sheets_urls":     sheets_urls,
        "access_sheets":   access_sheets,
        "sheets_data_url": sheets_data_url,
    }

def save_config(camps, sheets_url, sheets_urls=None, access_sheets=None, sheets_data_url=None):
    ensure_config_table()
    conn = get_db()
    cur  = conn.cursor()

    def upsert(key, value):
        cur.execute("""
            INSERT INTO app_config (key, value) VALUES (%s, %s)
            ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value
        """, (key, value))

    upsert('camps',       json.dumps(camps, ensure_ascii=False))
    upsert('sheets_url',  sheets_url or '')
    if sheets_urls is not None:
        upsert('sheets_urls', json.dumps(sheets_urls))
    if access_sheets is not None:
        upsert('access_sheets', json.dumps(access_sheets))
    if sheets_data_url is not None:
        upsert('sheets_data_url', sheets_data_url)

    conn.commit()
    cur.close(); conn.close()


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
            get_token_from_header(self.headers)
            self._send(json_response(get_config()))
        except (PermissionError, jwt.ExpiredSignatureError) as e:
            self._send(error_response(str(e), 401))
        except Exception as e:
            self._send(error_response(str(e), 500))

    def do_POST(self):
        try:
            user = get_token_from_header(self.headers)
            if user.get("role") != "admin":
                return self._send(error_response("Acesso negado.", 401))

            length = int(self.headers.get("Content-Length", 0))
            body   = json.loads(self.rfile.read(length))

            camps            = body.get("camps", [])
            sheets_url       = body.get("sheets_url", "")
            sheets_urls      = body.get("sheets_urls", {})
            access_sheets    = body.get("access_sheets", None)
            sheets_data_url  = body.get("sheets_data_url", None)

            if not isinstance(camps, list) or len(camps) != 3:
                return self._send(error_response("camps deve ser uma lista com 3 itens.", 400))

            save_config(camps, sheets_url, sheets_urls, access_sheets, sheets_data_url)
            self._send(json_response({"ok": True}))
        except (PermissionError, jwt.ExpiredSignatureError) as e:
            self._send(error_response(str(e), 401))
        except Exception as e:
            self._send(error_response(str(e), 500))

app = handler
