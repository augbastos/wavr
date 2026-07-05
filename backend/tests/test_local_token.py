"""A5.1 local-API token unit tests -- covers the untested 'auto' path: 256-bit
secret generation, persistence beside the db, one-time stdout disclosure, restart
reuse, the FS-error in-memory fallback, and the :memory:/empty db_path -> cwd rule.
No network, no real db -- pure filesystem via tmp_path."""
import wavr.local_token as lt
from wavr.local_token import resolve_local_token


def test_empty_cfg_disables(tmp_path):
    # Unset/empty => disabled => strict "" (byte-identical no-op upstream).
    assert resolve_local_token("", str(tmp_path / "wavr.db")) == ""
    assert resolve_local_token("   ", str(tmp_path / "wavr.db")) == ""


def test_literal_cfg_used_verbatim(tmp_path, capsys):
    assert resolve_local_token("s3cr3t-value", str(tmp_path / "wavr.db")) == "s3cr3t-value"
    # A literal token is NOT the 'auto' path -> nothing is printed.
    assert "Wavr local token" not in capsys.readouterr().out


def test_auto_creates_file_and_prints_exactly_once(tmp_path, capsys):
    db = tmp_path / "wavr.db"
    tok = resolve_local_token("auto", str(db))
    assert tok and len(tok) >= 32
    f = tmp_path / "local_token"
    assert f.exists()
    assert f.read_text(encoding="utf-8").strip() == tok
    out = capsys.readouterr().out
    assert out.count("Wavr local token:") == 1   # one-time disclosure, never repeated
    assert tok in out


def test_auto_reuses_persisted_token_across_restart(tmp_path, capsys):
    db = tmp_path / "wavr.db"
    t1 = resolve_local_token("auto", str(db))
    capsys.readouterr()
    t2 = resolve_local_token("auto", str(db))   # simulates a restart with same db dir
    assert t1 == t2                              # same persisted secret, not regenerated


def test_auto_falls_back_to_memory_on_write_failure(tmp_path, monkeypatch):
    db = tmp_path / "wavr.db"

    def boom(*a, **k):
        raise OSError("read-only filesystem")

    monkeypatch.setattr(lt.Path, "write_text", boom)
    tok = resolve_local_token("auto", str(db))   # must NOT raise
    assert tok and len(tok) >= 32                 # usable in-memory token returned
    assert not (tmp_path / "local_token").exists()


def test_auto_memory_dbpath_falls_back_to_cwd(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    tok = resolve_local_token("auto", ":memory:")
    assert tok
    assert (tmp_path / "local_token").exists()    # persisted in cwd, not beside a db


def test_auto_empty_dbpath_falls_back_to_cwd(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    tok = resolve_local_token("auto", "")
    assert tok
    assert (tmp_path / "local_token").exists()
