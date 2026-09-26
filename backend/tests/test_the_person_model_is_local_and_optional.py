"""A local ONNX model is selected only when both prerequisites are present."""
import hashlib
import importlib.util
import json
import os
import socket
import sys
import types
from pathlib import Path

import pytest

from wavr import person_onnx
from wavr.sources import camera


@pytest.fixture(autouse=True)
def reset_detector(monkeypatch):
    monkeypatch.setattr(camera, "_ORT_MODEL", None)
    monkeypatch.setattr(camera, "_ORT_PATH", None)
    monkeypatch.setattr(camera, "_MODEL_REASON", None)
    monkeypatch.setattr(camera, "_ORT_REFUSED", None)


def test_the_person_model_needs_both_ort_and_local_weights(tmp_path, monkeypatch, caplog):
    model = tmp_path / "yolov8n.onnx"
    monkeypatch.setenv("WAVR_PERSON_MODEL", str(model))
    calls = []
    monkeypatch.setattr(camera, "yolo_detect", lambda frame, conf: calls.append("legacy") or camera.Detection(1, 0.7))
    assert camera.person_detect(object()).count == 1
    assert "person model not provisioned" in caplog.text
    assert calls == ["legacy"]

    model.write_bytes(b"synthetic model")
    # A present file alone is insufficient; an import error must keep legacy.
    monkeypatch.setitem(sys.modules, "onnxruntime", None)
    assert camera.person_detect(object()).count == 1
    assert calls == ["legacy", "legacy"]

    monkeypatch.setitem(sys.modules, "onnxruntime", types.ModuleType("onnxruntime"))
    class FakeDetector:
        def __init__(self, path):
            self.path = path
        def persons(self, frame):
            return [0.8, 0.2]
    monkeypatch.setattr(person_onnx, "OrtDetector", FakeDetector)
    detected = camera.person_detect(object(), 0.4)
    assert detected == camera.Detection(1, 0.8)
    assert calls == ["legacy", "legacy"]


def test_missing_model_has_a_specific_reason_without_raising(tmp_path, monkeypatch, caplog):
    monkeypatch.setenv("WAVR_PERSON_MODEL", str(tmp_path / "missing.onnx"))
    monkeypatch.setattr(camera, "yolo_detect", lambda *_: camera.Detection(0, 0.0))
    assert camera.person_detect(object()) == camera.Detection(0, 0.0)
    assert "scripts/provision_person_model.py --pt" in caplog.text


def test_a_hash_mismatch_is_never_loaded_and_falls_back_once(tmp_path, monkeypatch, caplog):
    model = tmp_path / "yolov8n.onnx"
    model.write_bytes(b"corrupt")
    model.with_suffix(".onnx.json").write_text(json.dumps({"sha256": "0" * 64}))
    monkeypatch.setenv("WAVR_PERSON_MODEL", str(model))
    monkeypatch.setitem(sys.modules, "onnxruntime", types.ModuleType("onnxruntime"))
    checks = []
    def refuse(path):
        checks.append(path)
        person_onnx.verify_model(path)
        pytest.fail("a model that failed verification was loaded")
    monkeypatch.setattr(person_onnx, "OrtDetector", refuse)
    calls = []
    monkeypatch.setattr(camera, "yolo_detect", lambda frame, conf: calls.append("legacy") or camera.Detection(0, 0.0))
    for _ in range(3):
        assert camera.person_detect(object()) == camera.Detection(0, 0.0)
    assert calls == ["legacy"] * 3
    assert len(checks) == 1          # refused once, not re-hashed every frame
    assert caplog.text.count("SHA-256 mismatch; re-provision the local ONNX model; using the legacy detector") == 1


def test_a_sidecar_vouches_for_a_local_export_even_when_the_release_hash_is_pinned(tmp_path, monkeypatch):
    model = tmp_path / "yolov8n.onnx"
    model.write_bytes(b"a local export")
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"filename": model.name, "sha256": "0" * 64}))
    monkeypatch.setattr(person_onnx, "MANIFEST", manifest)
    model.with_suffix(".onnx.json").write_text(json.dumps({"sha256": hashlib.sha256(b"a local export").hexdigest()}))
    assert person_onnx.verify_model(model) == hashlib.sha256(b"a local export").hexdigest()
    # Without its sidecar the same file is a release copy, held to the pinned hash.
    model.with_suffix(".onnx.json").unlink()
    with pytest.raises(person_onnx.PersonModelError, match="SHA-256 mismatch"):
        person_onnx.verify_model(model)


def test_a_release_manifest_hash_mismatch_is_rejected(tmp_path, monkeypatch):
    model = tmp_path / "yolov8n.onnx"
    model.write_bytes(b"a different export")
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"filename": model.name, "sha256": "0" * 64}))
    monkeypatch.setattr(person_onnx, "MANIFEST", manifest)
    with pytest.raises(person_onnx.PersonModelError, match="SHA-256 mismatch"):
        person_onnx.verify_model(model)


def test_rectangular_preprocessing_matches_the_measured_path(monkeypatch):
    np = pytest.importorskip("numpy")
    if importlib.util.find_spec("cv2") is None:
        # The dev extra has numpy, not cv2. A 640-wide input needs no resize;
        # this small stand-in checks the exact border and tensor arithmetic.
        fake_cv2 = types.ModuleType("cv2")
        fake_cv2.BORDER_CONSTANT = 0
        fake_cv2.copyMakeBorder = lambda image, top, bottom, left, right, mode, value: np.pad(
            image, ((top, bottom), (left, right), (0, 0)), constant_values=114)
        monkeypatch.setitem(sys.modules, "cv2", fake_cv2)
    img = np.full((300, 640, 3), [10, 20, 30], dtype=np.uint8)
    tensor = person_onnx.letterbox(img)
    assert tensor.shape == (1, 3, 320, 640)  # square padding would be 640x640
    assert tensor.flags.c_contiguous
    assert np.allclose(tensor[0, :, 0, 0], [114, 114, 114] / np.array([255] * 3))
    assert np.allclose(tensor[0, :, 10, 0], [30, 20, 10] / np.array([255] * 3))
    # The benchmark must refer to this implementation, with no copied letterbox.
    from pathlib import Path
    benchmark = Path(__file__).resolve().parents[2] / "benchmarks" / "onnx_spike.py"
    source = benchmark.read_text(encoding="utf-8")
    assert "from wavr.person_onnx import OrtDetector, letterbox" in source
    assert "def letterbox(" not in source


def test_local_selection_does_not_open_a_socket(tmp_path, monkeypatch):
    model = tmp_path / "yolov8n.onnx"
    model.write_bytes(b"synthetic")
    model.with_suffix(".onnx.json").write_text(json.dumps({"sha256": hashlib.sha256(b"synthetic").hexdigest()}))
    monkeypatch.setenv("WAVR_PERSON_MODEL", str(model))
    monkeypatch.setitem(sys.modules, "onnxruntime", types.ModuleType("onnxruntime"))
    monkeypatch.setattr(person_onnx, "OrtDetector", lambda path: types.SimpleNamespace(persons=lambda frame: [0.9]))
    monkeypatch.setattr(socket.socket, "connect", lambda *_: pytest.fail("network call"))
    monkeypatch.setattr(socket, "create_connection", lambda *_a, **_k: pytest.fail("network call"))
    assert camera.person_detect(object()).count == 1


def test_the_detector_turns_ort_telemetry_off_before_the_session(tmp_path, monkeypatch):
    model = tmp_path / "yolov8n.onnx"
    model.write_bytes(b"synthetic")
    monkeypatch.delenv("ORT_DISABLE_TELEMETRY", raising=False)
    calls = []
    ort = types.ModuleType("onnxruntime")
    ort.disable_telemetry_events = lambda: calls.append("telemetry off")
    ort.SessionOptions = lambda: types.SimpleNamespace()
    def session(path, so, providers):
        calls.append(("session", os.environ.get("ORT_DISABLE_TELEMETRY")))
        return types.SimpleNamespace(get_inputs=lambda: [types.SimpleNamespace(name="images")])
    ort.InferenceSession = session
    monkeypatch.setitem(sys.modules, "onnxruntime", ort)
    # The release manifest pins the shipped model: a same-named file without its
    # sidecar is held to that hash and refused before ORT is touched.
    with pytest.raises(person_onnx.PersonModelError, match="SHA-256 mismatch"):
        person_onnx.OrtDetector(model)
    assert calls == []
    model.with_suffix(".onnx.json").write_text(json.dumps({"sha256": hashlib.sha256(b"synthetic").hexdigest()}))
    person_onnx.OrtDetector(model)
    assert calls == ["telemetry off", ("session", "1")]


def test_a_local_export_is_accepted_even_with_the_release_hash_pinned(tmp_path, monkeypatch):
    np = pytest.importorskip("numpy")
    # The exporter stamps the export time into the model, so a local export never equals
    # the release's pinned file. It must be vouched for by its own sidecar, not refused.
    for name in ("connect", "connect_ex", "sendto"):
        monkeypatch.setattr(socket.socket, name, getattr(socket.socket, name))
    monkeypatch.setattr(socket, "create_connection", socket.create_connection)
    monkeypatch.setattr(socket, "getaddrinfo", socket.getaddrinfo)
    spec = importlib.util.spec_from_file_location(
        "provision_person_model",
        Path(__file__).resolve().parents[2] / "scripts" / "provision_person_model.py")
    script = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(script)
    pt = tmp_path / "yolov8n.pt"
    pt.write_bytes(b"local weights")
    exported = tmp_path / "export" / "yolov8n.onnx"
    exported.parent.mkdir()
    exported.write_bytes(b"a local export, stamped with its own export time")
    monkeypatch.setattr(script, "import_yolo", lambda: lambda path: types.SimpleNamespace(export=lambda **_: str(exported)))
    session = types.SimpleNamespace(run=lambda _o, _i: [np.zeros((1, 84, 8400), dtype=np.float32)])
    monkeypatch.setattr(script, "OrtDetector", lambda path: types.SimpleNamespace(s=session, name="images"))
    dest = tmp_path / "models" / "yolov8n.onnx"
    monkeypatch.setenv("WAVR_PERSON_MODEL", str(dest))
    monkeypatch.setattr(sys, "argv", ["provision_person_model.py", "--pt", str(pt)])
    manifest = json.loads(person_onnx.MANIFEST.read_text(encoding="utf-8"))
    assert manifest["sha256"], "this test is about a PINNED release hash"
    assert script.main() == 0
    assert dest.read_bytes() == exported.read_bytes()
    sidecar = json.loads(dest.with_suffix(".onnx.json").read_text(encoding="utf-8"))
    assert sidecar["sha256"] == hashlib.sha256(exported.read_bytes()).hexdigest() != manifest["sha256"]
    assert person_onnx.verify_model(dest) == sidecar["sha256"]   # the camera will accept it
