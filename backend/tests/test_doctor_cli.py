"""wavr doctor CLI (`python -m wavr.doctor`): prints the Core's report, fails friendly.
No socket -- fetch_doctor is monkeypatched so the loop/exit logic is what's under test."""
import wavr.doctor as doctor


def test_cli_prints_report_and_exits_zero(monkeypatch, capsys):
    monkeypatch.setattr(doctor, "fetch_doctor",
                        lambda url, token=None, timeout=40.0: {"report": "Wavr doctor — report\nOK"})
    rc = doctor.main(["--url", "https://127.0.0.1:8000"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "Wavr doctor — report" in out


def test_cli_unreachable_core_is_friendly(monkeypatch, capsys):
    def _boom(url, token=None, timeout=40.0):
        raise OSError("connection refused")
    monkeypatch.setattr(doctor, "fetch_doctor", _boom)
    rc = doctor.main([])
    err = capsys.readouterr().err
    assert rc == 2
    assert "couldn't reach the Core" in err and "python -m wavr.serve" in err


def test_cli_missing_report_field_returns_3(monkeypatch, capsys):
    monkeypatch.setattr(doctor, "fetch_doctor", lambda url, token=None, timeout=40.0: {})
    rc = doctor.main([])
    assert rc == 3
