import hashlib, hmac, os, secrets
from itsdangerous import URLSafeTimedSerializer

# A random per-process fallback is suitable only for local development. Set SECRET_KEY in deployments.
SECRET = os.getenv("SECRET_KEY") or secrets.token_urlsafe(48)
_ser = URLSafeTimedSerializer(SECRET, salt="auth")


def hash_pw(pw: str, salt: str | None = None) -> str:
    salt = salt or secrets.token_hex(16)
    h = hashlib.pbkdf2_hmac("sha256", pw.encode(), salt.encode(), 200_000).hex()
    return f"{salt}${h}"


def check_pw(pw: str, stored: str) -> bool:
    salt = stored.split("$", 1)[0]
    return hmac.compare_digest(hash_pw(pw, salt), stored)


def make_token(uid: int, role: str) -> str:
    return _ser.dumps({"uid": uid, "role": role})


def read_token(token: str, max_age: int = 8 * 3600) -> dict:
    return _ser.loads(token, max_age=max_age)


def hash_secret(s: str) -> str:
    return hashlib.sha256((SECRET + s).encode()).hexdigest()
