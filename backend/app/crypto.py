import base64
import hashlib
import logging

from cryptography.fernet import Fernet, InvalidToken

from .config import settings

log = logging.getLogger("andro-cd.crypto")

# Fernet key derived from ENCRYPTION_KEY (or SESSION_SECRET). If neither is set
# explicitly, the key is random per process and stored secrets won't survive restarts.
_fernet = Fernet(base64.urlsafe_b64encode(
    hashlib.sha256(settings.encryption_key.encode()).digest()
))


class DecryptError(Exception):
    pass


def encrypt(value: str) -> str:
    return _fernet.encrypt(value.encode()).decode()


def decrypt(value: str) -> str:
    try:
        return _fernet.decrypt(value.encode()).decode()
    except InvalidToken:
        raise DecryptError(
            "cannot decrypt stored secret — was ENCRYPTION_KEY/SESSION_SECRET changed?"
        )
