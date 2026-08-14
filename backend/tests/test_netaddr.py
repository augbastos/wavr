import pytest
from wavr.netaddr import is_lan_ip


def test_is_lan_ip_accepts_private_literals():
    assert is_lan_ip("192.168.1.64") is True
    assert is_lan_ip("10.0.0.5") is True
    assert is_lan_ip("127.0.0.1") is True
    assert is_lan_ip("172.16.0.1") is True
    assert is_lan_ip("::1") is True
    assert is_lan_ip("fd00::1") is True
    assert is_lan_ip("fe80::1") is True


def test_is_lan_ip_rejects_public_and_dns():
    assert is_lan_ip("8.8.8.8") is False
    assert is_lan_ip("2001:4860:4860::8888") is False
    assert is_lan_ip("camera.local") is False   # DNS hostname refused
    assert is_lan_ip("example.com") is False
    assert is_lan_ip("") is False
    assert is_lan_ip(None) is False
    assert is_lan_ip("   ") is False


def test_is_lan_ip_rejects_cloud_metadata_despite_link_local():
    # 169.254.169.254 is link-local -> would pass the private/link-local allow;
    # explicitly denied (SSRF T2).
    assert is_lan_ip("169.254.1.1") is True       # ordinary link-local ok
    assert is_lan_ip("169.254.169.254") is False  # AWS IMDS denied
    assert is_lan_ip("fd00:ec2::254") is False    # IPv6 IMDS denied


def test_is_lan_ip_rejects_ipv4_mapped_metadata_bypass():
    # SSRF T2 bypass: ::ffff:169.254.169.254 routes to the IPv4 IMDS on a
    # dual-stack host but is not == the IPv4 metadata object. Must be denied,
    # via xaddr/rtsp entry points too, and a mapped public IP still non-LAN.
    assert is_lan_ip("::ffff:169.254.169.254") is False
    assert is_lan_ip("::ffff:8.8.8.8") is False   # mapped public -> non-LAN
    assert is_lan_ip("::ffff:192.168.1.64") is True  # mapped private ok


def test_is_lan_ip_handles_brackets():
    assert is_lan_ip("[192.168.1.64]") is True
    assert is_lan_ip("[::1]") is True
    assert is_lan_ip("[fd00::1]") is True
    assert is_lan_ip("[2001:4860:4860::8888]") is False
    assert is_lan_ip("[::ffff:169.254.169.254]") is False
