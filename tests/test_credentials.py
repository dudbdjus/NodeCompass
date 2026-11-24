from uuid import uuid4

import pytest

from proxy_manager.security.credentials import CredentialStore
from proxy_manager.security.dpapi import (
    PasswordProtectionError, protect_password, unprotect_password,
)


def test_native_credential_store_round_trip():
    store = CredentialStore(f"ProxyManager/test/{uuid4().hex}")
    try:
        assert store.load() is None
        first = store.save("fixture-secret")
        assert store.load() == first
        assert store.save("fixture-secret").profile_id == first.profile_id
        assert "fixture-secret" not in repr(first)
        assert store.save("replacement").profile_id != first.profile_id
        store.delete()
        assert store.load() is None
        store.delete()
    finally:
        store.delete()


def test_native_dpapi_and_invalid_ciphertext():
    password = "测试＋fake-password"
    encrypted = protect_password(password)
    assert password.encode() not in encrypted
    assert unprotect_password(encrypted) == password
    assert unprotect_password(protect_password(None)) is None
    with pytest.raises(PasswordProtectionError) as error:
        unprotect_password(b"bad-blob")
    assert error.value.__context__ is None
