from wavr.health_check import (
    DEFAULT_RESOLVERS,
    SEVERITY_CRITICAL,
    SEVERITY_DEGRADED,
    SEVERITY_MAJOR,
    SEVERITY_MINOR,
    SEVERITY_OK,
    check_health,
    compute_severity,
)


def _fn(result: bool):
    async def check():
        return result
    return check


def _raising():
    async def check():
        raise RuntimeError("no route to host")
    return check


# ---- compute_severity (pure) ------------------------------------------------

def test_all_up_is_ok():
    assert compute_severity(True, {"a": True, "b": True, "c": True}) == SEVERITY_OK


def test_gateway_down_is_critical_regardless_of_resolvers():
    assert compute_severity(False, {"a": True}) == SEVERITY_CRITICAL
    assert compute_severity(False, {}) == SEVERITY_CRITICAL


def test_all_resolvers_down_is_major():
    assert compute_severity(True, {"a": False, "b": False, "c": False}) == SEVERITY_MAJOR


def test_majority_resolvers_down_is_degraded():
    assert compute_severity(True, {"a": True, "b": False, "c": False}) == SEVERITY_DEGRADED


def test_single_resolver_down_is_minor():
    assert compute_severity(True, {"a": True, "b": True, "c": False}) == SEVERITY_MINOR


def test_single_extra_target_down_is_minor():
    assert compute_severity(True, {"a": True}, {"x": False}) == SEVERITY_MINOR


def test_multiple_extra_targets_down_is_degraded():
    assert compute_severity(True, {"a": True}, {"x": False, "y": False}) == SEVERITY_DEGRADED


def test_no_resolvers_configured_and_all_extras_up_is_ok():
    assert compute_severity(True, {}, {"x": True}) == SEVERITY_OK


# ---- check_health (async orchestration) -------------------------------------

async def test_check_health_all_up_shape():
    result = await check_health(
        _fn(True), gateway_host="192.168.1.1",
        resolver_checks={"1.1.1.1": _fn(True), "8.8.8.8": _fn(True)},
    )
    assert result == {
        "severity": "ok",
        "gateway": {"ok": True, "host": "192.168.1.1"},
        "resolvers": {"1.1.1.1": True, "8.8.8.8": True},
        "extra": {},
        "failed": [],
    }


async def test_check_health_reports_failed_names_gateway_first():
    result = await check_health(
        _fn(False), gateway_host="192.168.1.1",
        resolver_checks={"1.1.1.1": _fn(False), "8.8.8.8": _fn(True)},
        extra_checks={"nas.local": _fn(False)},
    )
    assert result["severity"] == SEVERITY_CRITICAL   # gateway down wins outright
    assert result["failed"] == ["gateway", "1.1.1.1", "nas.local"]


async def test_check_health_degraded_when_majority_resolvers_down():
    result = await check_health(
        _fn(True),
        resolver_checks={"1.1.1.1": _fn(False), "8.8.8.8": _fn(False), "9.9.9.9": _fn(True)},
    )
    assert result["severity"] == SEVERITY_DEGRADED


async def test_check_health_tolerates_raising_checks():
    result = await check_health(
        _raising(),
        resolver_checks={"1.1.1.1": _raising()},
    )
    assert result["gateway"]["ok"] is False
    assert result["resolvers"] == {"1.1.1.1": False}
    assert result["severity"] == SEVERITY_CRITICAL


async def test_check_health_defaults_to_the_three_public_resolvers_when_unset(monkeypatch):
    import wavr.health_check as mod

    async def always_true(*_a, **_k):
        return True
    monkeypatch.setattr(mod, "make_checker", lambda host: _fn(True))
    result = await check_health(_fn(True))
    assert set(result["resolvers"].keys()) == set(DEFAULT_RESOLVERS)
    assert result["severity"] == SEVERITY_OK
