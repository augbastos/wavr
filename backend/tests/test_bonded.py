import json

import pytest

from wavr.bonded import parse_windows, parse_linux, read_bonded


def test_parse_windows_extracts_dev_mac_and_name():
    raw = json.dumps([
        {"FriendlyName": "Alice's phone",
         "InstanceId": "BTHENUM\\Dev_AABBCCDDEEFF\\7&123&0"},
        {"FriendlyName": "Bluetooth Radio",           # adapter, no DEV_ -> skipped
         "InstanceId": "USB\\VID_8087&PID_0026\\5&x"},
    ])
    assert parse_windows(raw) == [
        {"address": "aa:bb:cc:dd:ee:ff", "name": "Alice's phone"},
    ]


def test_parse_windows_single_object_not_array():
    raw = json.dumps({"FriendlyName": "Watch",
                      "InstanceId": "BTHLE\\DEV_112233445566&x"})
    assert parse_windows(raw) == [{"address": "11:22:33:44:55:66", "name": "Watch"}]


def test_parse_windows_dedupes_and_survives_junk():
    raw = json.dumps([
        {"FriendlyName": "A", "InstanceId": "x\\DEV_AABBCCDDEEFF\\1"},
        {"FriendlyName": "A dup", "InstanceId": "y\\DEV_AABBCCDDEEFF\\2"},
        {"FriendlyName": "bad", "InstanceId": "DEV_ZZZZZZZZZZZZ"},   # not hex -> no match
    ])
    assert parse_windows(raw) == [{"address": "aa:bb:cc:dd:ee:ff", "name": "A"}]


def test_parse_windows_bad_json_is_empty():
    assert parse_windows("not json at all") == []
    assert parse_windows("") == []


def test_parse_linux_bluetoothctl():
    raw = ("Device AA:BB:CC:DD:EE:FF Guest phone\n"
           "Device 11:22:33:44:55:66 Watch\n"
           "garbage line\n")
    assert parse_linux(raw) == [
        {"address": "aa:bb:cc:dd:ee:ff", "name": "Guest phone"},
        {"address": "11:22:33:44:55:66", "name": "Watch"},
    ]


async def test_read_bonded_never_raises_on_runner_failure():
    async def boom(*args):
        raise FileNotFoundError("tool absent")
    assert await read_bonded(run=boom) == []      # degrades to [], never crashes
