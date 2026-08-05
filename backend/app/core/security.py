import base64
import hashlib
import hmac
import json
import secrets
import threading
import time
from typing import Any


PBKDF2_ITERATIONS = 600_000


class LoginThrottle:
    def __init__(self, max_failures: int = 5, window_seconds: int = 300):
        self.max_failures = max_failures
        self.window_seconds = window_seconds
        self._failures: dict[str, list[float]] = {}
        self._lock = threading.Lock()

    def is_allowed(self, key: str, now: float | None = None) -> bool:
        timestamp = time.monotonic() if now is None else now
        with self._lock:
            recent = [item for item in self._failures.get(key, []) if item > timestamp - self.window_seconds]
            self._failures[key] = recent
            return len(recent) < self.max_failures

    def record_failure(self, key: str, now: float | None = None) -> None:
        timestamp = time.monotonic() if now is None else now
        with self._lock:
            recent = [item for item in self._failures.get(key, []) if item > timestamp - self.window_seconds]
            recent.append(timestamp)
            self._failures[key] = recent
            if len(self._failures) > 10_000:
                self._failures = {item_key: values for item_key, values in self._failures.items() if values[-1] > timestamp - self.window_seconds}

    def reset(self, key: str) -> None:
        with self._lock:
            self._failures.pop(key, None)


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, PBKDF2_ITERATIONS)
    return f"pbkdf2_sha256${PBKDF2_ITERATIONS}${_b64(salt)}${_b64(digest)}"


def verify_password(password: str, encoded: str) -> bool:
    try:
        algorithm, iterations, salt, expected = encoded.split("$", 3)
        if algorithm != "pbkdf2_sha256":
            return False
        digest = hashlib.pbkdf2_hmac(
            "sha256", password.encode(), _unb64(salt), int(iterations)
        )
        return hmac.compare_digest(_b64(digest), expected)
    except (ValueError, TypeError):
        return False


def create_access_token(subject: str, secret: str, ttl_seconds: int = 8 * 3600) -> str:
    now = int(time.time())
    payload = {"sub": subject, "iat": now, "exp": now + ttl_seconds, "nonce": secrets.token_hex(8)}
    body = _b64(json.dumps(payload, separators=(",", ":")).encode())
    signature = _b64(hmac.new(secret.encode(), body.encode(), hashlib.sha256).digest())
    return f"{body}.{signature}"


def decode_access_token(token: str, secret: str) -> dict[str, Any] | None:
    try:
        body, signature = token.split(".", 1)
        expected = _b64(hmac.new(secret.encode(), body.encode(), hashlib.sha256).digest())
        if not hmac.compare_digest(signature, expected):
            return None
        payload = json.loads(_unb64(body))
        if int(payload["exp"]) < int(time.time()):
            return None
        return payload
    except (ValueError, KeyError, json.JSONDecodeError):
        return None


def _b64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode().rstrip("=")


def _unb64(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
