from base64 import b64decode, b64encode
from enum import Enum
from uuid import UUID

import pywintypes
import win32cred
from winerror import ERROR_NOT_FOUND

from proxy_manager.security.dpapi import PasswordProtectionError, protect_password, unprotect_password


_TARGET_PREFIX = "ProxyManager/SSH/v1"


class SecretKind(Enum):
    PASSWORD = "password"
    PASSPHRASE = "passphrase"


class SshCredentialError(RuntimeError):
    pass


def _target(profile: UUID, kind: SecretKind) -> str:
    if type(profile) is not UUID or type(kind) is not SecretKind:
        raise SshCredentialError("ssh_invalid_credential_target")
    return f"{_TARGET_PREFIX}/{profile}/{kind.value}"


class SshCredentialStore:
    """Windows Credential Manager entries containing user-scope DPAPI ciphertext."""

    def save(self, profile: UUID, kind: SecretKind, secret: str) -> None:
        target = _target(profile, kind)
        if type(secret) is not str or not secret:
            raise SshCredentialError("ssh_invalid_secret")
        try:
            ciphertext = protect_password(secret)
        except PasswordProtectionError:
            ciphertext = None
        if ciphertext is None:
            raise SshCredentialError("ssh_credential_encryption_failed")
        blob = b64encode(ciphertext).decode("ascii")
        if len(blob) * 2 > 2560:
            raise SshCredentialError("ssh_credential_too_long")
        try:
            win32cred.CredWrite({
                "TargetName": target,
                "Type": win32cred.CRED_TYPE_GENERIC,
                "Persist": win32cred.CRED_PERSIST_LOCAL_MACHINE,
                "CredentialBlob": blob,
            }, 0)
            return
        except pywintypes.error:
            pass
        raise SshCredentialError("ssh_credential_write_failed")

    def load(self, profile: UUID, kind: SecretKind) -> str | None:
        target = _target(profile, kind)
        try:
            record = win32cred.CredRead(target, win32cred.CRED_TYPE_GENERIC, 0)
            ciphertext = b64decode(record["CredentialBlob"].decode("utf-16-le"), validate=True)
            return unprotect_password(ciphertext)
        except pywintypes.error as error:
            if error.winerror == ERROR_NOT_FOUND:
                return None
        except (PasswordProtectionError, ValueError, KeyError, AttributeError, TypeError):
            pass
        raise SshCredentialError("ssh_credential_read_failed")

    def delete(self, profile: UUID, kind: SecretKind) -> None:
        target = _target(profile, kind)
        try:
            win32cred.CredDelete(target, win32cred.CRED_TYPE_GENERIC, 0)
            return
        except pywintypes.error as error:
            if error.winerror == ERROR_NOT_FOUND:
                return
        raise SshCredentialError("ssh_credential_delete_failed")
