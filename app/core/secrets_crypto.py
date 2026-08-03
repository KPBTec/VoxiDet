"""
core/secrets_crypto.py — Cifrado en reposo para API keys de proveedores ASR
guardadas en la base de datos (v1.16.0, ver CHANGELOG).

Fernet (cryptography, ya es dependencia del proyecto) — cifrado simétrico
autenticado. La clave (KEYS_ENCRYPTION_SECRET) vive en credentials.conf,
FUERA de la base de datos — un dump de MySQL solo no alcanza para leer las
keys reales, hace falta también el archivo de credenciales del servidor.

El valor en texto plano de una key nunca se loguea ni se vuelve a mostrar
completo en el panel una vez guardada — solo se descifra en memoria, justo
antes de llamar a la API del proveedor.
"""
from cryptography.fernet import Fernet, InvalidToken

from app.config import settings


def _fernet() -> Fernet:
    if not settings.KEYS_ENCRYPTION_SECRET:
        raise RuntimeError(
            "KEYS_ENCRYPTION_SECRET no está configurado en credentials.conf — "
            "no se pueden cifrar/descifrar API keys guardadas en el panel."
        )
    return Fernet(settings.KEYS_ENCRYPTION_SECRET.encode())


def encrypt_key(plaintext: str) -> str:
    return _fernet().encrypt(plaintext.encode()).decode()


def decrypt_key(ciphertext: str) -> str:
    try:
        return _fernet().decrypt(ciphertext.encode()).decode()
    except InvalidToken:
        # KEYS_ENCRYPTION_SECRET cambió o el dato está corrupto — no reventar
        # la rotación completa por una key ilegible, tratarla como ausente.
        return ""


def mask_key(plaintext: str) -> str:
    """Mismo criterio que _mask_key() en providers.py (para las keys de .env),
    unificado acá para las keys guardadas en DB."""
    if len(plaintext) <= 9:
        return "***"
    return plaintext[:5] + "..." + plaintext[-4:]
