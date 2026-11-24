from pathlib import Path
from threading import Event
from time import monotonic

from proxy_manager.monitoring.ssh_client import HostKeyConfirmationRequired, KeyAuth, PasswordAuth, SshClient, SshError
from proxy_manager.provider.kiwivm_client import KiwiVMClient, KiwiVMEndpoint
from proxy_manager.provider.kiwivm_parser import KiwiVMError
from proxy_manager.security.ssh_credentials import SecretKind, SshCredentialError, SshCredentialStore
from proxy_manager.security.vps_credentials import KiwiVMCredentialError, KiwiVMCredentialStore


_ENDPOINTS = {
    KiwiVMEndpoint.SERVICE_INFO: "get_service_info",
    KiwiVMEndpoint.LIVE_SERVICE_INFO: "get_live_service_info",
    KiwiVMEndpoint.RAW_USAGE_STATS: "get_raw_usage_stats",
}


class ApiSchedule:
    def __init__(self, clock=monotonic):
        self._clock = clock
        self._attempted = {}

    def due(self, profile, *, forced=False):
        now = self._clock()
        intervals = {KiwiVMEndpoint.SERVICE_INFO: 1800, KiwiVMEndpoint.LIVE_SERVICE_INFO: profile.live_minutes * 60,
                     KiwiVMEndpoint.RAW_USAGE_STATS: 3600}
        return tuple(endpoint for endpoint, seconds in intervals.items()
                     if forced or now >= self._attempted.get(endpoint, -float("inf")) + seconds)

    def started(self, endpoints):
        self._attempted.update(dict.fromkeys(endpoints, self._clock()))


def refresh_kiwi(repo, profile, endpoints, cancel: Event) -> dict:
    try:
        credential = KiwiVMCredentialStore().load(profile.veid)
    except KiwiVMCredentialError:
        return {"credential": "credential_read_failed"}
    if credential is None:
        return {"credential": "no_kiwi_credentials"}
    outcome = {}
    with KiwiVMClient(credential, cancel=cancel) as client:
        for endpoint in endpoints:
            if cancel.is_set():
                break
            try:
                snapshot = getattr(client, _ENDPOINTS[endpoint])()
                if cancel.is_set():
                    break
                stored = repo.save_kiwi(profile.id, endpoint, snapshot)
                outcome[endpoint] = "success" if stored else "unrecognized_history"
            except KiwiVMError as error:
                if cancel.is_set():
                    break
                repo.record(profile.id, endpoint.value, error.kind)
                outcome[endpoint] = error.kind
    return outcome


def ssh_auth(profile):
    if not profile.username:
        raise SshCredentialError("ssh_username_required")
    store = SshCredentialStore()
    if profile.auth_method == "private_key":
        if not profile.key_path:
            raise SshCredentialError("ssh_private_key_unavailable")
        return KeyAuth(profile.username, Path(profile.key_path),
                       store.load(profile.credential_id, SecretKind.PASSPHRASE))
    secret = store.load(profile.credential_id, SecretKind.PASSWORD)
    if secret is None:
        raise SshCredentialError("ssh_password_required")
    return PasswordAuth(profile.username, secret)


class SshConnectionJob:
    def __init__(self, repo, profile, approved, *, persist=False):
        self.repo = repo
        self.profile = profile
        self.approved = approved
        self.persist = persist
        self.client = SshClient(profile.endpoint)
        self.cancelled = Event()

    def cancel(self):
        self.cancelled.set()
        self.client.interrupt()

    def run(self):
        try:
            if self.cancelled.is_set():
                return "cancelled", None
            if self.approved is None:
                self.client.connect(None)
                return "ssh_host_confirmation_required", None
            if self.persist:
                self.repo.trust(self.profile, self.approved)
            self.client.connect(self.approved, ssh_auth(self.profile))
            self.repo.record(self.profile.id, "ssh_test", "success")
            return "success", None
        except HostKeyConfirmationRequired as error:
            return "confirm", error.observed
        except (SshError, SshCredentialError) as error:
            if not self.cancelled.is_set():
                self.repo.record(self.profile.id, "ssh_test", str(error))
            return str(error), None
        except ValueError:
            return "ssh_host_key_changed", None
        finally:
            self.client.close()
