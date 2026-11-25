"""Read-only Xray routing evidence. No data-plane traffic or DNS queries."""
from dataclasses import dataclass, field, replace
import json
import re
from time import monotonic

import httpx

from proxy_manager.monitoring.ssh_client import SshClient, SshError
from proxy_manager.provider.panel_client import PanelError, _get_json
from proxy_manager.services.geoip import normalize_ip, public_ip
from proxy_manager.services.vps_jobs import ssh_auth

MAX_CLIENTS = 200
PROBES = ("8.8.8.8", "1.1.1.1")
PANEL_READS = {
    "clients/list": "GET",
    "inbounds/list/slim": "GET",
    "xray/routeTest": "POST",
}


class ReadError(ValueError):
    pass


def panel_read(credential, path, *, data=None, transport=None, session=None):
    if path not in PANEL_READS or (data is not None) != (path == "xray/routeTest"):
        raise ReadError("panel_method_not_allowed")
    if data is not None and (
        not isinstance(data, dict) or set(data) != {"ip", "port", "network", "protocol", "email", "inboundTag"}
        or data["ip"] not in PROBES or data["port"] != "443" or data["network"] != "tcp"
        or data["protocol"] != "tls" or not _label(data["email"]) or not _label(data["inboundTag"])
    ):
        raise ReadError("route_context_not_allowed")
    result = _get_json(credential, path, method=PANEL_READS[path], data=data, transport=transport, session=session,
                       max_bytes=32768 if data is not None else 1024 * 1024)
    if not isinstance(result, dict) or result.get("success") is not True or "obj" not in result:
        raise ReadError("panel_response")
    return result["obj"]


def _label(value):
    return isinstance(value, str) and 0 < len(value) <= 512 and value.isprintable()


def record_ids(credential, *, transport=None):
    rows = panel_read(credential, "clients/list", transport=transport)
    if not isinstance(rows, list) or len(rows) > MAX_CLIENTS:
        raise ReadError("inventory_shape")
    pairs = {}
    seen = set()
    for row in rows:
        user, ident = row.get("email"), row.get("id")
        if not _label(user) or type(ident) is not int or ident <= 0 or user in pairs or ident in seen:
            raise ReadError("inventory_id")
        pairs[user] = ident
        seen.add(ident)
    return pairs


def inventory(read):
    inbounds, clients = read("inbounds/list/slim"), read("clients/list")
    if not isinstance(inbounds, list) or not isinstance(clients, list) or len(clients) > MAX_CLIENTS:
        raise ReadError("inventory_shape")
    local = {}
    for row in inbounds:
        if row.get("enable") is True and row.get("nodeId") is None and _label(row.get("tag")):
            settings = row.get("settings")
            settings = json.loads(settings) if isinstance(settings, str) else settings
            members = {c.get("email") for c in (settings or {}).get("clients", []) if c.get("enable") is True}
            local[row["id"]] = row["tag"], members
    result = {}
    for row in clients:
        ident, user, attached = row.get("id"), row.get("email"), row.get("inboundIds")
        if type(ident) is not int or ident <= 0 or not _label(user) or ident in result or not isinstance(attached, list):
            raise ReadError("inventory_id")
        tags = tuple(sorted({local[i][0] for i in attached if i in local and user in local[i][1]}))
        result[ident] = (user, tags if len(tags) == len(attached) else ())
    return result


@dataclass(frozen=True)
class Outbound:
    protocol: str
    servers: tuple
    chain: bool


@dataclass(frozen=True)
class Finding:
    status: str
    ip: str = ""
    conditional: bool = False
    outbound_tag: str = ""
    live_only: bool = False


@dataclass(frozen=True)
class Result:
    status: str
    rows: dict = field(default_factory=dict, repr=False)
    users: dict = field(default_factory=dict, repr=False)
    diagnostic: str = ""


def _outbounds(rows):
    result = {}
    for tag, proto, servers, chain in rows:
        if not isinstance(tag, str) or tag in result or not isinstance(proto, str) or type(chain) is not bool:
            raise ReadError("outbound_shape")
        if not all(isinstance(a, (list, tuple)) and len(a) == 2 for a in servers):
            raise ReadError("outbound_servers")
        result[tag] = Outbound(proto, tuple((a, p) for a, p in servers), chain)
    return result


def classify(outbound, conditional=False):
    if outbound is None or outbound.chain:
        return Finding("ambiguous", conditional=conditional)
    if outbound.protocol == "blackhole":
        return Finding("blocked", conditional=conditional)
    if outbound.protocol == "freedom":
        return Finding("direct", conditional=conditional)
    if outbound.protocol not in {"socks", "http"} or len(outbound.servers) != 1:
        return Finding("ambiguous", conditional=conditional)
    raw, port = outbound.servers[0]
    if type(port) is not int or not 1 <= port <= 65535 or not _label(raw):
        return Finding("ambiguous", conditional=conditional)
    if raw.startswith("[") and raw.endswith("]"):
        raw = raw[1:-1]
    ip = normalize_ip(raw)
    if ip is not None:
        return Finding("located", str(ip), conditional) if public_ip(ip) else Finding("nonpublic", conditional=conditional)
    try:
        domain = raw.removesuffix(".").encode("idna").decode("ascii")
    except UnicodeError:
        return Finding("ambiguous", conditional=conditional)
    if (not re.fullmatch(r"[0-9.]+", domain) and len(domain) <= 253
            and all(re.fullmatch(r"[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?", part)
                    for part in domain.split("."))):
        return Finding("domain", conditional=conditional)
    return Finding("ambiguous", conditional=conditional)


def ordinary_route(rules, user, inbound):
    """Conservative ordered check of disk rules for ordinary public TLS.

    A selector that could split this class before a definite match is not
    resolved by trying a few targets. Private-IP/other-protocol exceptions
    only mark conditional routing, not a second ordinary public endpoint.
    """
    conditional = False
    for rule in rules:
        users = rule.get("user")
        if users and user not in users:
            if any(v.startswith(("regexp:", "regex:")) for v in users):
                return "ambiguous", True
            continue
        if rule.get("inboundTag") and inbound not in rule["inboundTag"]:
            continue
        if rule.get("protocol") and "tls" not in rule["protocol"]:
            conditional = True
            continue
        if rule.get("ip") == ["geoip:private"]:
            conditional = True
            continue
        if any(rule.get(k) for k in ("ip", "domain", "port", "network", "unsupported", "balancerTag")):
            return "ambiguous", True
        tag = rule.get("outboundTag")
        if not _label(tag):
            return "ambiguous", True
        return tag, conditional
    return None, conditional


def route_decision(read, ip, user, tag):
    data = read("xray/routeTest", data={"ip": ip, "port": "443", "network": "tcp",
                "protocol": "tls", "email": user, "inboundTag": tag})
    if not isinstance(data, dict) or type(data.get("matched")) is not bool:
        raise ReadError("route_shape")
    if data.get("groupTags"):
        return "ambiguous"
    if data["matched"] is False:
        return None
    if not _label(data.get("outboundTag")):
        raise ReadError("route_tag")
    return data["outboundTag"]


def reconcile(snapshot, second, clients, again, findings):
    if snapshot != second or clients != again:
        return Result("changed")
    return Result("ok", findings, {ident: user for ident, (user, _) in clients.items()})


def _snapshot(client):
    snapshot = json.loads(client.read_xray_snapshot())
    if not isinstance(snapshot, dict) or snapshot.get("stage") != "ok":
        raise ReadError("snapshot_unavailable")
    return snapshot


def inspect_panel(credential, profile, pin, cancel, *, ssh_factory=SshClient, transport=None, read_auth=ssh_auth):
    """Fail closed: a failed/changed refresh returns no old mappings."""
    if pin is None or not profile.endpoint:
        return Result("ssh_unavailable")
    if cancel.is_set():
        return Result("unavailable")
    client = ssh_factory(profile.endpoint)
    session = httpx.Client(timeout=5, verify=True, follow_redirects=False, trust_env=False, transport=transport)
    stop = monotonic() + 65
    def check():
        if cancel.is_set() or monotonic() >= stop:
            raise ReadError("cancelled")
    def read(path, **kwargs):
        check()
        return panel_read(credential, path, session=session, **kwargs)
    try:
        client.connect(pin, read_auth(profile))
        check()
        before = _snapshot(client)
        live, disk = _outbounds(before["runtime"]), _outbounds(before["disk"])
        default_tag = before["disk"][0][0] if disk else None
        clients = inventory(read)
        findings = {}
        live_checks = {}
        for ident, (user, tags) in clients.items():
            check()
            if len(tags) != 1 or before["script"]:
                findings[ident] = Finding("ambiguous")
                continue
            tag = tags[0]
            expected, conditional = ordinary_route(before["rules"], user, tag)
            if expected == "ambiguous":
                findings[ident] = Finding("ambiguous", conditional=True)
                continue
            votes = {route_decision(read, ip, user, tag) for ip in PROBES}
            if votes != {expected}:
                # The running core can gain per-client routes after startup.
                # Accept only a consistent live decision with a known, simple
                # proxy when the file has no explicit matching rule.
                selected = next(iter(votes)) if len(votes) == 1 else None
                finding = classify(live.get(selected), conditional=True)
                if expected is None and selected not in {None, "ambiguous"} and finding.status == "located":
                    findings[ident] = replace(finding, outbound_tag=selected, live_only=True)
                    live_checks[ident] = (user, tag, selected)
                else:
                    findings[ident] = Finding("ambiguous", conditional=True)
            elif expected is None:
                cfg = live.get(default_tag)
                state = ("default_direct" if before["started_from_file"] and cfg is not None
                         and cfg == disk.get(default_tag) and cfg.protocol == "freedom" and not cfg.chain
                         else "ambiguous")
                findings[ident] = Finding(state, conditional=conditional)
            else:
                findings[ident] = replace(classify(live.get(expected), conditional), outbound_tag=expected)
        again = inventory(read)
        check()
        for ident, (user, inbound, selected) in live_checks.items():
            if {route_decision(read, ip, user, inbound) for ip in PROBES} != {selected}:
                findings[ident] = Finding("ambiguous", conditional=True)
        second = _snapshot(client)
        check()
        return reconcile(before, second, clients, again, findings)
    except (ReadError, PanelError, SshError) as error:
        return Result("unavailable", diagnostic=str(error))
    except Exception:
        return Result("unavailable", diagnostic="shape_or_credentials")
    finally:
        session.close()
        client.close()
