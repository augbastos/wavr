"""F3 camera IP-drift detection (wavr.camera_health)."""
from wavr.camera_health import CameraHealthMonitor, suggest_rebind
from wavr.netinventory import Device


def _dev(mac, ip, vendor="TP-Link"):
    return Device(mac=mac, ip=ip, vendor=vendor, device_type="camera", known=True)

def _cam(name="cam_q", mac="aa:bb:cc:dd:ee:ff", url="rtsp://u:p@10.0.0.5/s1"):
    return {"name": name, "room": "quarto", "rtsp_url": url, "confidence": 0.5, "mac": mac}


# ---- suggest_rebind: pure, never guesses ----------------------------------------

def test_suggest_none_when_mac_unset():
    assert suggest_rebind(_cam(mac=None), [_dev("aa:bb:cc:dd:ee:ff", "10.0.0.9")]) is None

def test_suggest_none_when_mac_absent_from_inventory():
    assert suggest_rebind(_cam(), [_dev("11:22:33:44:55:66", "10.0.0.9")]) is None

def test_suggest_none_when_ip_unchanged():
    # MAC present, but still at the stored IP -> no drift.
    assert suggest_rebind(_cam(url="rtsp://u:p@10.0.0.5/s1"),
                          [_dev("aa:bb:cc:dd:ee:ff", "10.0.0.5")]) is None

def test_suggest_none_for_hostname_url():
    # hostname rtsp URLs are out of scope for drift (DNS re-resolves them).
    assert suggest_rebind(_cam(url="rtsp://u:p@cam.local/s1"),
                          [_dev("aa:bb:cc:dd:ee:ff", "10.0.0.9")]) is None

def test_suggest_returns_dict_on_drift():
    sug = suggest_rebind(_cam(url="rtsp://u:p@10.0.0.5/s1"),
                         [_dev("aa:bb:cc:dd:ee:ff", "10.0.0.9", vendor="TP-Link")])
    assert sug["camera"] == "cam_q"
    assert sug["mac"] == "aa:bb:cc:dd:ee:ff"
    assert sug["current_ip"] == "10.0.0.5"
    assert sug["suggested_ip"] == "10.0.0.9"
    assert sug["vendor"] == "TP-Link"       # so the user can judge the suggestion
    assert "ts" in sug
    # SECURITY: the rtsp_url (creds) is NEVER surfaced in a suggestion.
    assert "rtsp_url" not in sug
    assert "secret" not in str(sug) and "@" not in str(sug)

def test_suggest_case_insensitive_mac_match():
    sug = suggest_rebind(_cam(mac="aa:bb:cc:dd:ee:ff"),
                         [_dev("AA:BB:CC:DD:EE:FF", "10.0.0.9")])
    assert sug is not None and sug["suggested_ip"] == "10.0.0.9"


# ---- CameraHealthMonitor: edge-trigger + clear ----------------------------------

def test_monitor_folds_suggestion_on_down_report():
    cam = _cam(url="rtsp://u:p@10.0.0.5/s1")
    inv = [_dev("aa:bb:cc:dd:ee:ff", "10.0.0.9")]
    m = CameraHealthMonitor(get_camera=lambda n: cam, latest_inventory=lambda: inv)
    m.report("cam_q", False)
    [sug] = m.suggestions()
    assert sug["suggested_ip"] == "10.0.0.9"

def test_monitor_no_suggestion_when_no_drift():
    cam = _cam(url="rtsp://u:p@10.0.0.5/s1")
    inv = [_dev("aa:bb:cc:dd:ee:ff", "10.0.0.5")]     # same IP -> no drift
    m = CameraHealthMonitor(get_camera=lambda n: cam, latest_inventory=lambda: inv)
    m.report("cam_q", False)
    assert m.suggestions() == []

def test_monitor_recovery_drops_suggestion():
    cam = _cam(url="rtsp://u:p@10.0.0.5/s1")
    inv = [_dev("aa:bb:cc:dd:ee:ff", "10.0.0.9")]
    m = CameraHealthMonitor(get_camera=lambda n: cam, latest_inventory=lambda: inv)
    m.report("cam_q", False)
    assert len(m.suggestions()) == 1
    m.report("cam_q", True)                            # camera recovered
    assert m.suggestions() == []

def test_monitor_clear_drops_suggestion_after_rebind():
    cam = _cam(url="rtsp://u:p@10.0.0.5/s1")
    inv = [_dev("aa:bb:cc:dd:ee:ff", "10.0.0.9")]
    m = CameraHealthMonitor(get_camera=lambda n: cam, latest_inventory=lambda: inv)
    m.report("cam_q", False)
    m.clear("cam_q")
    assert m.suggestions() == []

def test_monitor_edge_triggers_one_suggestion_per_camera():
    cam = _cam(url="rtsp://u:p@10.0.0.5/s1")
    inv = [_dev("aa:bb:cc:dd:ee:ff", "10.0.0.9")]
    m = CameraHealthMonitor(get_camera=lambda n: cam, latest_inventory=lambda: inv)
    m.report("cam_q", False)
    m.report("cam_q", False)                           # repeated down -> still one entry
    assert len(m.suggestions()) == 1

def test_monitor_tolerates_store_error():
    def boom(_name):
        raise RuntimeError("db locked")
    m = CameraHealthMonitor(get_camera=boom, latest_inventory=lambda: [])
    m.report("cam_q", False)                           # must not raise
    assert m.suggestions() == []

def test_monitor_ring_is_bounded():
    inv = lambda: [_dev("aa:bb:cc:dd:ee:ff", "10.0.0.9")]
    # each distinct camera name (all drifting to the same MAC/IP) folds a suggestion;
    # the ring caps at max_suggestions, evicting oldest.
    m = CameraHealthMonitor(get_camera=lambda n: _cam(name=n), latest_inventory=inv,
                            max_suggestions=3)
    for i in range(6):
        m.report(f"cam_{i}", False)
    assert len(m.suggestions()) == 3


# ---- down(): the liveness latch getter (feeds /api/cameras tri-state) ------------

def test_down_getter_tracks_latch():
    # A down report latches the name; recovery and clear() each release it. Carries
    # NAMES only -- never a frame, rtsp_url or credential (ADR-0002).
    m = CameraHealthMonitor(get_camera=lambda n: None, latest_inventory=lambda: [])
    assert m.down() == []
    m.report("cam_q", False)
    assert m.down() == ["cam_q"]
    assert all(isinstance(n, str) for n in m.down())       # names only
    m.report("cam_q", True)                                # recovered
    assert m.down() == []
    m.report("cam_q", False)
    m.clear("cam_q")                                       # rebound
    assert m.down() == []
