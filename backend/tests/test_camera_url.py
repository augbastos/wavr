"""F3 pure rtsp-URL host helpers (wavr.camera_url)."""
from wavr.camera_url import rtsp_host, rebind_rtsp_host


# ---- rtsp_host: extract host, strip scheme/creds/port/path ----------------------

def test_rtsp_host_plain():
    assert rtsp_host("rtsp://10.0.0.5/stream1") == "10.0.0.5"

def test_rtsp_host_strips_creds_and_port_and_path():
    assert rtsp_host("rtsp://user:pass@10.0.0.5:554/stream1?x=1") == "10.0.0.5"

def test_rtsp_host_rtsps_scheme():
    assert rtsp_host("rtsps://user:pass@192.168.1.64:322/live") == "192.168.1.64"

def test_rtsp_host_bracketed_ipv6():
    assert rtsp_host("rtsp://user:pass@[fd00::1]:554/s") == "fd00::1"

def test_rtsp_host_odd_shapes_return_none_without_raising():
    assert rtsp_host("notaurl") is None            # no ://
    assert rtsp_host("") is None
    assert rtsp_host("rtsp://") is None            # empty authority
    assert rtsp_host("rtsp://user:pass@/path") is None   # creds but no host


# ---- rebind_rtsp_host: swap ONLY the host, preserve everything else --------------

def test_rebind_preserves_scheme_creds_port_path():
    out = rebind_rtsp_host("rtsp://user:pass@10.0.0.5:554/stream1", "10.0.0.9")
    assert out == "rtsp://user:pass@10.0.0.9:554/stream1"

def test_rebind_no_port_no_path():
    assert rebind_rtsp_host("rtsp://10.0.0.5", "10.0.0.9") == "rtsp://10.0.0.9"

def test_rebind_no_creds_keeps_port_and_query():
    out = rebind_rtsp_host("rtsp://10.0.0.5:8554/s?tcp", "192.168.1.2")
    assert out == "rtsp://192.168.1.2:8554/s?tcp"

def test_rebind_password_with_special_chars_is_untouched():
    # host surgery is string-based (no urllib decode/re-encode) so a password with
    # reserved chars survives byte-for-byte.
    url = "rtsp://user:p%40ss:word@10.0.0.5:554/s1"
    out = rebind_rtsp_host(url, "10.0.0.9")
    assert out == "rtsp://user:p%40ss:word@10.0.0.9:554/s1"

def test_rebind_rtsps_scheme_preserved():
    assert rebind_rtsp_host("rtsps://u:p@10.0.0.5/s", "10.0.0.9") == "rtsps://u:p@10.0.0.9/s"

def test_rebind_odd_shape_returns_original_unchanged():
    assert rebind_rtsp_host("notaurl", "10.0.0.9") == "notaurl"      # no :// -> unchanged
    assert rebind_rtsp_host("rtsp://user@/path", "10.0.0.9") == "rtsp://user@/path"  # no host
    assert rebind_rtsp_host("rtsp://10.0.0.5", "") == "rtsp://10.0.0.5"   # empty new_ip
