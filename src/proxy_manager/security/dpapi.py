import pywintypes
import win32crypt
from win32cryptcon import CRYPTPROTECT_UI_FORBIDDEN


class PasswordProtectionError(Exception):
    pass


def protect_password(password: str | None) -> bytes | None:
    if password is None:
        return None
    try:
        return win32crypt.CryptProtectData(
            password.encode("utf-8"), "Proxy Manager", None, None, None,
            CRYPTPROTECT_UI_FORBIDDEN,
        )
    except (pywintypes.error, UnicodeError):
        pass
    raise PasswordProtectionError("password_encryption_failed")


def unprotect_password(ciphertext: bytes | None) -> str | None:
    if ciphertext is None:
        return None
    try:
        _, plaintext = win32crypt.CryptUnprotectData(
            ciphertext, None, None, None, CRYPTPROTECT_UI_FORBIDDEN,
        )
        return plaintext.decode("utf-8")
    except (pywintypes.error, UnicodeError):
        pass
    raise PasswordProtectionError("password_decryption_failed")
