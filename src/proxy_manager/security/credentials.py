from dataclasses import dataclass, field
from hmac import compare_digest
from uuid import UUID, uuid4

import pywintypes
import win32cred
from winerror import ERROR_NOT_FOUND

from proxy_manager.provider.client import validate_key
from proxy_manager.provider.errors import ProviderError

TARGET = "ProxyManager/ProxySeller/v2"


class CredentialError(Exception):
    pass


@dataclass(frozen=True)
class StoredCredential:
    profile_id: str
    key: str = field(repr=False)


class CredentialStore:
    def __init__(self, target: str = TARGET):
        self._target = target

    def load(self) -> StoredCredential | None:
        try:
            record = win32cred.CredRead(self._target, win32cred.CRED_TYPE_GENERIC, 0)
            profile_id = UUID(record["UserName"]).hex
            key = record["CredentialBlob"].decode("utf-16-le")
            validate_key(key)
            return StoredCredential(profile_id, key)
        except pywintypes.error as error:
            if error.winerror == ERROR_NOT_FOUND:
                return None
        except (ValueError, KeyError, AttributeError, TypeError, ProviderError):
            pass
        raise CredentialError("credential_read_failed")

    def save(self, key: str) -> StoredCredential:
        validate_key(key)
        encoded = key.encode("utf-16-le")
        if len(encoded) > 2560:
            raise CredentialError("credential_too_long")
        previous = self.load()
        if previous is not None and compare_digest(encoded, previous.key.encode("utf-16-le")):
            return previous
        profile = StoredCredential(uuid4().hex, key)
        try:
            win32cred.CredWrite({
                "TargetName": self._target,
                "Type": win32cred.CRED_TYPE_GENERIC,
                "Persist": win32cred.CRED_PERSIST_LOCAL_MACHINE,
                "UserName": profile.profile_id,
                "CredentialBlob": key,
            }, 0)
            return profile
        except pywintypes.error:
            pass
        raise CredentialError("credential_write_failed")

    def delete(self) -> None:
        try:
            win32cred.CredDelete(self._target, win32cred.CRED_TYPE_GENERIC, 0)
            return
        except pywintypes.error as error:
            if error.winerror == ERROR_NOT_FOUND:
                return
        raise CredentialError("credential_delete_failed")
