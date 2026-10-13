"""Minimal crypto helpers for vault encryption.

Uses PBKDF2-HMAC-SHA256 to derive a 32-byte key and AES-GCM for authenticated
encryption. Blobs are JSON-wrapped with base64-encoded salt, iv, and ciphertext.

This module performs lazy imports of the `cryptography` package and raises a
clear error if it's not available.
"""
import os
import json
import base64
from typing import Any


def _ensure_cryptography():
    try:
        from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        return PBKDF2HMAC, hashes, AESGCM
    except Exception as e:
        raise RuntimeError("Missing 'cryptography' library. Install with: pip install cryptography")


def derive_key(passphrase: str, salt: bytes, iterations: int = 200_000) -> bytes:
    PBKDF2HMAC, hashes, _ = _ensure_cryptography()
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=32,
        salt=salt,
        iterations=iterations,
    )
    return kdf.derive(passphrase.encode())


def encrypt_json(obj: Any, passphrase: str) -> str:
    PBKDF2HMAC, hashes, AESGCM = _ensure_cryptography()
    salt = os.urandom(16)
    key = derive_key(passphrase, salt)
    aesgcm = AESGCM(key)
    iv = os.urandom(12)
    plaintext = json.dumps(obj, separators=(",", ":")).encode("utf-8")
    ct = aesgcm.encrypt(iv, plaintext, None)

    wrapper = {
        "_enc": True,
        "ver": 1,
        "salt": base64.b64encode(salt).decode('ascii'),
        "iv": base64.b64encode(iv).decode('ascii'),
        "ct": base64.b64encode(ct).decode('ascii')
    }
    return json.dumps(wrapper)


def decrypt_json(blob_text: str, passphrase: str) -> Any:
    PBKDF2HMAC, hashes, AESGCM = _ensure_cryptography()
    wrapper = json.loads(blob_text)
    if not wrapper.get("_enc"):
        raise ValueError("Blob is not encrypted")
    salt = base64.b64decode(wrapper["salt"])
    iv = base64.b64decode(wrapper["iv"])
    ct = base64.b64decode(wrapper["ct"])
    key = derive_key(passphrase, salt)
    aesgcm = AESGCM(key)
    pt = aesgcm.decrypt(iv, ct, None)
    return json.loads(pt.decode('utf-8'))


def is_encrypted_blob(text: str) -> bool:
    try:
        wrapper = json.loads(text)
        return isinstance(wrapper, dict) and wrapper.get("_enc") is True
    except Exception:
        return False
