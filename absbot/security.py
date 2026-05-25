from __future__ import annotations

import secrets
import string


PASSWORD_ALPHABET = string.ascii_letters + string.digits
CODE_ALPHABET = string.ascii_uppercase + string.digits


def generate_password(length: int = 14) -> str:
    return "".join(secrets.choice(PASSWORD_ALPHABET) for _ in range(length))


def generate_code(prefix: str = "", length: int = 10) -> str:
    body = "".join(secrets.choice(CODE_ALPHABET) for _ in range(length))
    return f"{prefix}{body}".upper()


def normalize_code(code: str) -> str:
    return code.strip().upper()

