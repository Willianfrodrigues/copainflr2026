import os, json, jwt, hashlib, psycopg2
from datetime import datetime, timedelta
from google.cloud import bigquery
from google.oauth2 import service_account

# ── ENV VARS ─────────────────────────────────────────────────
# Read lazily inside functions so missing vars raise at call time, not import time
def _env(key, default=None):
    val = os.environ.get(key, default)
    if val is None:
        raise RuntimeError(f"Missing required env var: {key}")
    return val

BQ_PROJECT = os.environ.get("BQ_PROJECT", "looker-integrations-402615")
BQ_TABLE   = os.environ.get("BQ_TABLE",   "looker-integrations-402615.tiktok_ads.conjunto mesclado 3")

# ── NEON (PostgreSQL) ────────────────────────────────────────
def get_db():
    return psycopg2.connect(_env("NEON_DATABASE_URL"), sslmode="require")

def init_db():
    """Cria tabela de usuários se não existir"""
    conn = get_db()
    cur  = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id         SERIAL PRIMARY KEY,
            username   TEXT UNIQUE NOT NULL,
            password   TEXT NOT NULL,
            role       TEXT NOT NULL DEFAULT 'client',
            client     TEXT,
            campaigns  TEXT[]  DEFAULT '{}'
        )
    """)
    cur.execute("""
        INSERT INTO users (username, password, role, client, campaigns)
        VALUES (%s, %s, 'admin', 'inflr Admin', '{}')
        ON CONFLICT (username) DO NOTHING
    """, ('admin', _hash('admin123')))
    cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS exclude TEXT[] DEFAULT '{}'")
    conn.commit()
    cur.close(); conn.close()

# ── AUTH ─────────────────────────────────────────────────────
def _hash(pw: str) -> str:
    return hashlib.sha256(pw.encode()).hexdigest()

def verify_user(username: str, password: str):
    conn = get_db()
    cur  = conn.cursor()
    cur.execute(
        "SELECT username, role, client, campaigns, exclude FROM users WHERE username=%s AND password=%s",
        (username, _hash(password))
    )
    row = cur.fetchone()
    cur.close(); conn.close()
    if not row:
        return None
    return {"username": row[0], "role": row[1], "client": row[2], "campaigns": list(row[3] or []), "exclude": list(row[4] or [])}

def create_token(user: dict) -> str:
    payload = {
        "sub":       user["username"],
        "role":      user["role"],
        "client":    user["client"],
        "campaigns": user["campaigns"],
        "exclude":   user.get("exclude", []),
        "exp":       datetime.utcnow() + timedelta(hours=12)
    }
    return jwt.encode(payload, os.environ.get("JWT_SECRET","inflr@2026#segredo!"), algorithm="HS256")

def decode_token(token: str) -> dict:
    return jwt.decode(token, os.environ.get("JWT_SECRET","inflr@2026#segredo!"), algorithms=["HS256"])

def get_token_from_header(headers) -> dict:
    """Decode JWT from Authorization header. Raises on invalid/missing token."""
    auth = headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        raise PermissionError("Token não encontrado.")
    try:
        return decode_token(auth[7:])
    except jwt.ExpiredSignatureError:
        raise
    except jwt.PyJWTError as e:
        raise PermissionError(f"Token inválido: {e}")

# ── BIGQUERY ─────────────────────────────────────────────────
def get_bq():
    sa_json = os.environ.get("BQ_SERVICE_ACCOUNT_JSON", "")
    if sa_json:
        info  = json.loads(sa_json)
        creds = service_account.Credentials.from_service_account_info(info)
        return bigquery.Client(credentials=creds, project=BQ_PROJECT)
    return bigquery.Client(project=BQ_PROJECT)

def build_campaign_filter(user: dict) -> str:
    """Filtra por palavras-chave do usuário. Vazio = vê tudo. Admin = vê tudo.
    Separar por vírgula = OR. Usar + dentro de uma keyword = AND.
    Ex: 'REDE, BRA BET+COPA' → REDE OR (BRA BET AND COPA)
    """
    if user["role"] == "admin":
        return "1=1"
    keywords = [k.strip().upper() for k in user.get("campaigns", []) if k.strip()]
    if not keywords:
        return "1=1"
    
    or_conditions = []
    for kw in keywords:
        kw = kw.replace("'", "''").replace("\\", "\\\\")
        if '+' in kw:
            # AND: todos os termos devem estar presentes
            parts = [p.strip() for p in kw.split('+') if p.strip()]
            and_cond = " AND ".join([f"UPPER(CAMPAIGN_NAME) LIKE '%{p}%'" for p in parts])
            or_conditions.append(f"({and_cond})")
        else:
            or_conditions.append(f"UPPER(CAMPAIGN_NAME) LIKE '%{kw}%'")
    
    return f"({' OR '.join(or_conditions)})"

# ── RESPONSE HELPERS ─────────────────────────────────────────
def cors_headers():
    return {
        "Access-Control-Allow-Origin":  "*",
        "Access-Control-Allow-Methods": "GET, POST, PUT, DELETE, OPTIONS",
        "Access-Control-Allow-Headers": "Authorization, Content-Type",
        "Content-Type": "application/json"
    }

def json_response(data, status=200):
    return {
        "statusCode": status,
        "headers":    cors_headers(),
        "body":       json.dumps(data, default=str)
    }

def error_response(msg, status=400):
    return json_response({"error": msg}, status)
