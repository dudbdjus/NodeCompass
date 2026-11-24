from base64 import b64decode
from uuid import uuid4

import pytest
import win32cred

from proxy_manager.security import ssh_credentials as credential
from proxy_manager.security.ssh_credentials import SecretKind, SshCredentialError, SshCredentialStore


def test_password_and_passphrase_are_separate_dpapi_ciphertexts(monkeypatch):
    prefix = f"ProxyManager/TestSSH/{uuid4()}"
    monkeypatch.setattr(credential, "_TARGET_PREFIX", prefix)
    profile = uuid4()
    store = SshCredentialStore()
    try:
        for kind, secret in [(SecretKind.PASSWORD, "密码-a"), (SecretKind.PASSPHRASE, "口令-b")]:
            store.save(profile, kind, secret)
            record = win32cred.CredRead(f"{prefix}/{profile}/{kind.value}", win32cred.CRED_TYPE_GENERIC)
            blob = record["CredentialBlob"]
            assert secret.encode("utf-16-le") not in blob
            ciphertext = b64decode(blob.decode("utf-16-le"), validate=True)
            assert secret.encode("utf-8") not in ciphertext
            assert store.load(profile, kind) == secret
            assert store.load(uuid4(), kind) is None
        store.delete(profile, SecretKind.PASSWORD)
        assert store.load(profile, SecretKind.PASSWORD) is None
        assert store.load(profile, SecretKind.PASSPHRASE) == "口令-b"
    finally:
        for kind in SecretKind:
            store.delete(profile, kind)


def test_credential_errors_do_not_include_input():
    with pytest.raises(SshCredentialError, match="^ssh_invalid_credential_target$"):
        SshCredentialStore().save(uuid4(), "dangerous/target", "confidential")
