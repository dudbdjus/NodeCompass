from base64 import b64decode, b64encode
import json

import pywintypes
import win32cred
from winerror import ERROR_NOT_FOUND

from proxy_manager.provider.panel_client import PanelCredentials, PanelError
from proxy_manager.security.dpapi import PasswordProtectionError, protect_password, unprotect_password


class PanelCredentialStore:
    def __init__(self, target="ProxyManager/3x-ui/v1"):
        self.target = target

    def save(self, credentials):
        try:
            secret = protect_password(json.dumps([credentials.base_url, credentials.token]))
            blob = b64encode(secret).decode("ascii")
            if len(blob) * 2 <= 2560:
                win32cred.CredWrite({
                    "TargetName": self.target, "Type": win32cred.CRED_TYPE_GENERIC,
                    "Persist": win32cred.CRED_PERSIST_LOCAL_MACHINE, "CredentialBlob": blob,
                }, 0)
                return
        except (PasswordProtectionError, pywintypes.error):
            pass
        raise PanelError("save")

    def load(self):
        try:
            record = win32cred.CredRead(self.target, win32cred.CRED_TYPE_GENERIC, 0)
            ciphertext = b64decode(record["CredentialBlob"].decode("utf-16-le"), validate=True)
            address, token = json.loads(unprotect_password(ciphertext))
            return PanelCredentials(address, token)
        except pywintypes.error as error:
            if error.winerror == ERROR_NOT_FOUND:
                return None
        except (PasswordProtectionError, ValueError, KeyError, AttributeError, TypeError):
            pass
        raise PanelError("load")

    def delete(self):
        try:
            win32cred.CredDelete(self.target, win32cred.CRED_TYPE_GENERIC, 0)
            return
        except pywintypes.error as error:
            if error.winerror == ERROR_NOT_FOUND:
                return
        raise PanelError("delete")
