from wavr.sources.network import parse_arp_table

WINDOWS_ARP = """
Interface: 192.168.0.10 --- 0x5
  Internet Address      Physical Address      Type
  192.168.0.1           AA-BB-CC-DD-EE-FF     dynamic
  192.168.0.23          11-22-33-44-55-66     dynamic
  192.168.0.255         ff-ff-ff-ff-ff-ff     static
"""

def test_parse_arp_table_extracts_normalized_macs():
    macs = parse_arp_table(WINDOWS_ARP)
    assert "aa:bb:cc:dd:ee:ff" in macs
    assert "11:22:33:44:55:66" in macs
    assert "ff:ff:ff:ff:ff:ff" in macs
    assert len(macs) == 3

def test_parse_arp_table_handles_colon_form_and_empty():
    assert parse_arp_table("host 0a:1b:2c:3d:4e:5f ok") == {"0a:1b:2c:3d:4e:5f"}
    assert parse_arp_table("") == set()
    assert parse_arp_table("no macs here 12-34") == set()
