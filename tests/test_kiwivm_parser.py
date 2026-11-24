from datetime import UTC, datetime
from decimal import Decimal

import pytest

from proxy_manager.provider.kiwivm_parser import (
    KiwiVMError, parse_live_service_info, parse_service_info, parse_usage_history,
)


def test_billing_cycle_uses_reset_and_scales_both_raw_byte_fields():
    parsed = parse_service_info({
        "error": "0", "hostname": "vps", "vm_type": "kvm", "plan_ram": "1024", "plan_swap": 0,
        "plan_monthly_data": "1000", "data_counter": 251, "monthly_data_multiplier": "1.25",
        "data_next_reset": 1704067201, "ip_addresses": ["192.0.2.1", "bad", "2001:db8::1/64"],
        "suspended": "0", "policy_violation": 1,
    })
    assert parsed.plan_ram == 1024 and parsed.plan_swap == 0
    assert parsed.ip_addresses == ("192.0.2.1", "2001:db8::1/64")
    assert not parsed.suspended and parsed.policy_violation
    traffic = parsed.billing
    assert (traffic.raw_quota_bytes, traffic.raw_used_bytes) == (1000, 251)
    assert traffic.quota_bytes == Decimal("1250.00")
    assert traffic.used_bytes == Decimal("313.75")
    assert traffic.remaining_bytes == Decimal("936.25")
    assert traffic.usage_percent == Decimal("25.1")
    assert traffic.reset_at_utc == datetime(2024, 1, 1, 0, 0, 1, tzinfo=UTC)
    assert traffic.reset_timestamp == 1704067201


@pytest.mark.parametrize("multiplier", [None, "invalid", 0, -1, "NaN", "Infinity", True])
def test_invalid_or_missing_multiplier_keeps_raw_bytes_without_estimate(multiplier):
    billing = parse_service_info({
        "error": 0, "plan_monthly_data": 100, "data_counter": 20,
        "monthly_data_multiplier": multiplier,
    }).billing
    assert billing.raw_used_bytes == 20
    assert billing.quota_bytes is None and billing.used_bytes is None
    assert billing.usage_percent is None and billing.remaining_bytes is None


@pytest.mark.parametrize(("quota", "used", "remaining", "percentage"), [
    (0, 10, 0, None), (None, 10, None, None), (100, None, None, None),
    (100, 120, 0, 120), (100, 0, 200, 0),
])
def test_zero_absent_and_overage_handling(quota, used, remaining, percentage):
    billing = parse_service_info({
        "error": 0, "plan_monthly_data": quota, "data_counter": used,
        "monthly_data_multiplier": 2,
    }).billing
    assert billing.remaining_bytes == remaining
    assert billing.usage_percent == percentage


def test_missing_fields_and_unknown_identity_do_not_invent_values():
    info = parse_service_info({"error": 0, "data": {"hostname": "vps", "email": "hidden"}})
    assert info.hostname == "vps"
    assert info.vm_type is None and info.ip_addresses is None
    assert info.billing.reset_at_utc is None
    assert "hidden" not in repr(info)


def test_live_fields_allow_partial_and_keep_provider_units():
    parsed = parse_live_service_info({
        "error": 0, "hostname": "vps", "ve_status": "Running", "ssh_port": "22",
        "ve_used_disk_space_b": "120", "ve_disk_quota_gb": "8.5", "mem_available_kb": 512,
        "swap_total_kb": 0, "is_disk_throttled": 1, "load_average": "0.12 0.10 0.02",
        "screendump_png_base64": "do-not-retain", "private_key": "do-not-retain",
    })
    assert parsed.service.hostname == "vps"
    assert parsed.ve_status == "Running" and parsed.ssh_port == 22
    assert parsed.ve_disk_quota_gb == Decimal("8.5")
    assert parsed.ve_used_disk_space_b == 120 and parsed.mem_available_kb == 512
    assert parsed.swap_total_kb == 0 and parsed.is_disk_throttled
    assert parsed.is_cpu_throttled is None and parsed.swap_available_kb is None
    assert "do-not-retain" not in repr(parsed)


@pytest.mark.parametrize("payload", [
    None, [], {"error": False}, {"error": 0.0}, {"error": 0, "plan_disk": True},
    {"error": 0, "data": {"not_a_known_field": "x"}},
])
def test_malformed_or_useless_service_response_is_rejected(payload):
    with pytest.raises(KiwiVMError, match="invalid_response"):
        parse_service_info(payload)


def test_history_keeps_raw_points_and_only_observes_spacing():
    row = {"timestamp": 1700000000, "network_in_bytes": 900, "network_out_bytes": 801,
           "cpu_usage": "1.25", "disk_read_bytes": 17, "disk_write_bytes": 18}
    result = parse_usage_history({"error": 0, "data": [
        {**row, "timestamp": 1700000060, "network_in_bytes": 2},
        row, row, {"timestamp": "broken", "network_in_bytes": 12},
    ]})
    assert result.traffic_semantics == "unconfirmed"
    assert result.observed_interval_seconds == 60
    assert result.duplicate_rows == 1 and result.ignored_rows == 1
    before, after = result.points
    assert before.at_utc == datetime.fromtimestamp(1700000000, UTC)
    assert (before.network_in_bytes, after.network_in_bytes) == (900, 2)
    assert before.network_out_bytes == 801 and before.cpu_usage == Decimal("1.25")
    assert (before.disk_read_bytes, before.disk_write_bytes) == (17, 18)


def test_timestamp_map_handles_irregular_spacing_without_guessing():
    result = parse_usage_history({"error": 0, "data": {
        "1700000000": {"network_in_bytes": "5"},
        "1700000030": {"network_out_bytes": 4},
        "1700000090": {"network_in_bytes": 9},
    }})
    assert result.layout == "timestamp_map"
    assert len(result.points) == 3 and result.observed_interval_seconds is None
    assert result.points[1].network_in_bytes is None


@pytest.mark.parametrize(("data", "recognized"), [
    ([], True), ({}, True), ([[1700000000, 45]], False),
    ({"unidentified": {"timestamp": 1700000000, "network_in_bytes": 10}}, False),
    (None, False),
])
def test_history_empty_and_unknown_shapes(data, recognized):
    result = parse_usage_history({"error": 0, "data": data})
    assert result.format_recognized is recognized
    assert not result.points and result.observed_interval_seconds is None


def test_conflicting_history_points_are_preserved_as_raw_data():
    response = parse_usage_history({"error": 0, "data": [
        {"timestamp": 1700000000, "network_in_bytes": 9},
        {"timestamp": 1700000000, "network_in_bytes": 15},
    ]})
    assert [point.network_in_bytes for point in response.points] == [9, 15]
    assert response.duplicate_rows == 0
    assert response.observed_interval_seconds is None
