import json
from http.server import BaseHTTPRequestHandler
from _helpers import verify_user, create_token, init_db, json_response, error_response, cors_headers

class handler(BaseHTTPRequestHandler):

    def do_OPTIONS(self):
        self.send_response(200)
        for k, v in cors_headers().items():
            self.send_header(k, v)
        self.end_headers()

    def do_POST(self):
        try:
            init_db()  # garante que a tabela existe
            length = int(self.headers.get("Content-Length", 0))
            body   = json.loads(self.rfile.read(length))

            user = verify_user(body.get("username", ""), body.get("password", ""))
            if not user:
                resp = error_response("Usuário ou senha incorretos.", 401)
            else:
                token = create_token(user)
                resp  = json_response({
                    "token":     token,
                    "username":  user["username"],
                    "role":      user["role"],
                    "client":    user["client"],
                    "campaigns": user["campaigns"]
                })

            self.send_response(resp["statusCode"])
            for k, v in resp["headers"].items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(resp["body"].encode())

        except Exception as e:
            resp = error_response(str(e), 500)
            self.send_response(500)
            for k, v in resp["headers"].items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(resp["body"].encode())
