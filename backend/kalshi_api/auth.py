import base64
import os
import time
from pathlib import Path

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding


WS_PATH = "/trade-api/ws/v2"


class KalshiAuthError(Exception):
    pass


def load_local_env(base_dir):
    env_path = Path(base_dir) / ".env"
    if not env_path.exists():
        return

    lines = env_path.read_text(encoding="utf-8").splitlines()
    i = 0
    while i < len(lines):
        line = lines[i].strip()
        i += 1
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        # Multi-line double-quoted value: opening quote with no closing quote on same line
        if value.startswith('"') and not (len(value) > 1 and value.endswith('"')):
            collected = [value[1:]]          # strip the opening quote
            while i < len(lines):
                chunk = lines[i]
                i += 1
                if chunk.rstrip().endswith('"'):
                    collected.append(chunk.rstrip()[:-1])   # strip closing quote
                    break
                collected.append(chunk)
            value = "\n".join(collected)
        else:
            value = value.strip('"').strip("'")
        os.environ.setdefault(key, value)


def get_api_key_id():
    return os.environ.get("KALSHI_API_KEY_ID", "").strip()


def get_private_key():
    private_key_pem = os.environ.get("KALSHI_PRIVATE_KEY_PEM", "").strip()
    private_key_path = os.environ.get("KALSHI_PRIVATE_KEY_PATH", "").strip()

    if private_key_pem:
        key_bytes = private_key_pem.replace("\\n", "\n").encode("utf-8")
    elif private_key_path:
        key_bytes = Path(private_key_path).read_bytes()
    else:
        raise KalshiAuthError(
            "Missing KALSHI_PRIVATE_KEY_PATH or KALSHI_PRIVATE_KEY_PEM."
        )

    return serialization.load_pem_private_key(key_bytes, password=None)


def sign_pss_text(private_key, text):
    signature = private_key.sign(
        text.encode("utf-8"),
        padding.PSS(
            mgf=padding.MGF1(hashes.SHA256()),
            salt_length=padding.PSS.DIGEST_LENGTH,
        ),
        hashes.SHA256(),
    )
    return base64.b64encode(signature).decode("utf-8")


def create_websocket_headers():
    api_key_id = get_api_key_id()
    if not api_key_id:
        raise KalshiAuthError("Missing KALSHI_API_KEY_ID.")

    private_key = get_private_key()
    timestamp = str(int(time.time() * 1000))
    signature = sign_pss_text(private_key, timestamp + "GET" + WS_PATH)

    return {
        "KALSHI-ACCESS-KEY": api_key_id,
        "KALSHI-ACCESS-SIGNATURE": signature,
        "KALSHI-ACCESS-TIMESTAMP": timestamp,
    }
