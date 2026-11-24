from base64 import b64decode, b64encode
from dataclasses import dataclass, field

import pywintypes
import win32cred
from winerror import ERROR_NOT_FOUND

from proxy_manager.security.dpapi import PasswordProtectionError, protect_password, unprotect_password

_TARGET_PREFIX = "ProxyManager/KiwiVM/v1"
_MAX_BLOB_BYTES = 2560


class KiwiVMCredentialError(Exception):
    """Credential failure carrying a safe error code only."""


def _veid(value: str | int) -> str:
    if type(value) is int and 0 < value < 10**20:
        return str(value)
    if type(value) is str and value.isascii() and value.isdecimal():
        canonical = value.lstrip("0")
        if 1 <= len(canonical) <= 20:
            return canonical
    raise KiwiVMCredentialError("invalid_veid")


@dataclass(frozen=True)
class KiwiVMCredential:
    veid: str
    api_key: str = field(repr=False)

    def __post_init__(self):
        object.__setattr__(self, "veid", _veid(self.veid))
        key = self.api_key
        if type(key) is not str or not key or not key.isprintable() or any(ch.isspace() for ch in key):
            raise KiwiVMCredentialError("invalid_api_key")


class KiwiVMCredentialStore:
    """Use a separate per-VEID Windows entry containing only DPAPI ciphertext."""

    def save(self, veid: str | int, api_key: str) -> KiwiVMCredential:
        credential = KiwiVMCredential(_veid(veid), api_key)
        try:
            encrypted = protect_password(credential.api_key)
        except PasswordProtectionError:
            encrypted = None
        if encrypted is None:
            raise KiwiVMCredentialError("credential_encryption_failed")
        blob = b64encode(encrypted).decode("ascii")
        if len(blob) * 2 > _MAX_BLOB_BYTES:
            raise KiwiVMCredentialError("credential_too_long")
        try:
            win32cred.CredWrite({
                "TargetName": f"{_TARGET_PREFIX}/{credential.veid}",
                "Type": win32cred.CRED_TYPE_GENERIC,
                "Persist": win32cred.CRED_PERSIST_LOCAL_MACHINE,
                "UserName": credential.veid,
                "CredentialBlob": blob,
            }, 0)
            return credential
        except pywintypes.error:
            pass
        raise KiwiVMCredentialError("credential_write_failed")

    def load(self, veid: str | int) -> KiwiVMCredential | None:
        canonical = _veid(veid)
        try:
            record = win32cred.CredRead(f"{_TARGET_PREFIX}/{canonical}", win32cred.CRED_TYPE_GENERIC, 0)
            ciphertext = b64decode(record["CredentialBlob"].decode("utf-16-le"), validate=True)
            return KiwiVMCredential(canonical, unprotect_password(ciphertext))
        except pywintypes.error as error:
            if error.winerror == ERROR_NOT_FOUND:
                return None
        except (PasswordProtectionError, KiwiVMCredentialError, ValueError, KeyError, AttributeError, TypeError):
            pass
        raise KiwiVMCredentialError("credential_read_failed")

    def delete(self, veid: str | int) -> None:
        canonical = _veid(veid)
        try:
            win32cred.CredDelete(f"{_TARGET_PREFIX}/{canonical}", win32cred.CRED_TYPE_GENERIC, 0)
            return
        except pywintypes.error as error:
            if error.winerror == ERROR_NOT_FOUND:
                return
        raise KiwiVMCredentialError("credential_delete_failed")
