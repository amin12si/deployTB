"""Encrypt/decrypt Railway API tokens before they touch disk.

ENCRYPTION_KEY must be a Fernet key (44-char urlsafe-base64 string).
Generate one with: python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"

Keep the same ENCRYPTION_KEY across redeploys of this bot — the backup/
import feature stores tokens still encrypted, and a different key means
an old backup can't be decrypted on a new deploy.
"""

import os
from cryptography.fernet import Fernet, InvalidToken

_key = os.environ.get("ENCRYPTION_KEY")
if not _key:
    raise RuntimeError("ENCRYPTION_KEY env var is required (see crypto.py docstring to generate one)")

_fernet = Fernet(_key.encode() if isinstance(_key, str) else _key)


def encrypt(plaintext: str) -> str:
    return _fernet.encrypt(plaintext.encode()).decode()


def decrypt(token: str) -> str:
    try:
        return _fernet.decrypt(token.encode()).decode()
    except InvalidToken:
        raise ValueError("could not decrypt — wrong ENCRYPTION_KEY for this data")
