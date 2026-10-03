"""Assinatura AES-CMAC das requisições do gateway (seção 7.2).

Mensagem assinada (UTF-8): "{gateway_id}\n{timestamp}\n{nonce}\n{sha256_hex(corpo_bruto)}"
"""

import hashlib

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import cmac
from cryptography.hazmat.primitives.ciphers import algorithms


def signing_message(gateway_id: int | str, timestamp: int | str, nonce: str, body: bytes) -> bytes:
    body_hash = hashlib.sha256(body).hexdigest()
    return f"{gateway_id}\n{timestamp}\n{nonce}\n{body_hash}".encode("utf-8")


def sign(secret_key: bytes, gateway_id: int | str, timestamp: int | str, nonce: str, body: bytes) -> str:
    mac = cmac.CMAC(algorithms.AES(secret_key))
    mac.update(signing_message(gateway_id, timestamp, nonce, body))
    return mac.finalize().hex()


def verify(secret_key: bytes, gateway_id: int | str, timestamp: int | str, nonce: str, body: bytes, signature_hex: str) -> bool:
    """Compara em tempo constante (cmac.verify)."""
    try:
        signature = bytes.fromhex(signature_hex)
    except ValueError:
        return False
    if len(signature) != 16:
        return False
    mac = cmac.CMAC(algorithms.AES(secret_key))
    mac.update(signing_message(gateway_id, timestamp, nonce, body))
    try:
        mac.verify(signature)
        return True
    except InvalidSignature:
        return False
