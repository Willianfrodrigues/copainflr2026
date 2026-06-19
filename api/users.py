import json, hashlib
import jwt
from http.server import BaseHTTPRequestHandler
from _helpers import (get_db, get_token_from_header,
                      json_response, error_response, cors_headers)

def _hash(pw): return hashlib.sha256(pw.encode()).hexdigest()

def _require_admin(headers):
    user = get_token_from_header(headers)
    if user.get("role") != "admin":
        raise PermissionError("Acesso negado.")
    return user

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
            _require_admin(self.headers)
            conn = get_db(); cur = conn.cursor()
            cur.execute("SELECT username, role, client, campaigns, exclude FROM users ORDER BY id")
            rows = [{"username":r[0],"role":r[1],"client":r[2],"campaigns":list(r[3] or []),"exclude":list(r[4] or [])} for r in cur.fetchall()]
            cur.close(); conn.close()
            self._send(json_response(rows))
        except (PermissionError, jwt.ExpiredSignatureError) as e:
            self._send(error_response(str(e), 401))
        except Exception as e:
            self._send(error_response(str(e), 500))

    def do_POST(self):
        try:
            _require_admin(self.headers)
            length = int(self.headers.get("Content-Length", 0))
            body   = json.loads(self.rfile.read(length))

            conn = get_db(); cur = conn.cursor()
            cur.execute("""
                INSERT INTO users (username, password, role, client, campaigns, exclude)
                VALUES (%s, %s, 'client', %s, %s, %s)
                ON CONFLICT (username) DO NOTHING
                RETURNING id
            """, (
                body["username"],
                _hash(body["password"]),
                body.get("client", ""),
                body.get("campaigns", []),
                body.get("exclude", [])
            ))
            inserted = cur.fetchone()
            conn.commit(); cur.close(); conn.close()

            if not inserted:
                return self._send(error_response("Usuário já existe.", 400))
            self._send(json_response({"ok": True}))
        except (PermissionError, jwt.ExpiredSignatureError) as e:
            self._send(error_response(str(e), 401))
        except Exception as e:
            self._send(error_response(str(e), 500))

    def do_PUT(self):
        try:
            _require_admin(self.headers)
            length = int(self.headers.get("Content-Length", 0))
            body   = json.loads(self.rfile.read(length))
            username  = body.get("username")
            campaigns = body.get("campaigns", [])
            exclude   = body.get("exclude", [])
            client    = body.get("client", "")
            conn = get_db(); cur = conn.cursor()
            cur.execute("UPDATE users SET campaigns=%s, exclude=%s, client=%s WHERE username=%s",
                        (campaigns, exclude, client, username))
            conn.commit(); cur.close(); conn.close()
            self._send(json_response({"ok": True}))
        except (PermissionError, jwt.ExpiredSignatureError) as e:
            self._send(error_response(str(e), 401))
        except Exception as e:
            self._send(error_response(str(e), 500))

    def do_DELETE(self):
        try:
            _require_admin(self.headers)
            length = int(self.headers.get("Content-Length", 0))
            body   = json.loads(self.rfile.read(length))
            username = body.get("username")

            if username == "admin":
                return self._send(error_response("Não é possível remover o admin.", 400))

            conn = get_db(); cur = conn.cursor()
            cur.execute("DELETE FROM users WHERE username=%s", (username,))
            conn.commit(); cur.close(); conn.close()
            self._send(json_response({"ok": True}))
        except (PermissionError, jwt.ExpiredSignatureError) as e:
            self._send(error_response(str(e), 401))
        except Exception as e:
            self._send(error_response(str(e), 500))
