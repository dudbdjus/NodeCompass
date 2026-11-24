from base64 import b64decode
from uuid import uuid4

import pytest

from proxy_manager.security import vps_credentials
from proxy_manager.security.dpapi import unprotect_password
from proxy_manager.security.vps_credentials import (
    KiwiVMCredential, KiwiVMCredentialError, KiwiVMCredentialStore,
)

SECRET = "fixture-vps-only+/key"


def test_windows_vault_stores_dpapi_ciphertext_in_an_isolated_target(monkeypatch):
    prefix = f"ProxyManager/test/KiwiVM/{uuid4().hex}"
    monkeypatch.setattr(vps_credentials, "_TARGET_PREFIX", prefix)
    vault = vps_credentials.win32cred
    store = KiwiVMCredentialStore()
    try:
        assert store.load("100") is None
        saved = store.save("00100", SECRET)
        assert saved.veid == "100" and SECRET not in repr(saved)
        assert store.load(100) == saved
        assert store.load("101") is None

        record = vault.CredRead(prefix + "/100", vault.CRED_TYPE_GENERIC, 0)
        wire = record["CredentialBlob"]
        assert SECRET.encode("utf-16-le") not in wire
        ciphertext = b64decode(wire.decode("utf-16-le"), validate=True)
        assert SECRET.encode() not in ciphertext
        assert unprotect_password(ciphertext) == SECRET

        store.save("101", "separate-test-key")
        store.delete("100")
        store.delete("100")
        assert store.load("100") is None
        assert store.load("101").api_key == "separate-test-key"
    finally:
        for veid in ("100", "101"):
            store.delete(veid)


@pytest.mark.parametrize("veid", ["0", "-1", "100/../../", "１００", False, "https://example.invalid/1"])
def test_invalid_veid_cannot_address_other_vault_entries(veid):
    with pytest.raises(KiwiVMCredentialError, match="invalid_veid"):
        KiwiVMCredentialStore().delete(veid)


@pytest.mark.parametrize("key", ["", " ", "key\nunsafe", "key with space", None])
def test_invalid_key_has_no_sensitive_exception(key):
    with pytest.raises(KiwiVMCredentialError) as error:
        KiwiVMCredential("100", key)
    assert str(error.value) == "invalid_api_key"


def test_corrupted_blob_error_is_redacted(monkeypatch):
    def corrupted(*_):
        return {"CredentialBlob": ("corrupted " + SECRET).encode("utf-16-le")}

    monkeypatch.setattr(vps_credentials.win32cred, "CredRead", corrupted)
    with pytest.raises(KiwiVMCredentialError) as error:
        KiwiVMCredentialStore().load("100")
    assert str(error.value) == "credential_read_failed"
    assert error.value.__context__ is None


def test_overlong_blob_is_rejected_before_vault_write(monkeypatch):
    def unexpected_write(*_):
        pytest.fail("overlong ciphertext must not reach the credential manager")

    monkeypatch.setattr(vps_credentials.win32cred, "CredWrite", unexpected_write)
    with pytest.raises(KiwiVMCredentialError, match="credential_too_long"):
        KiwiVMCredentialStore().save("100", "A" * 2000)
