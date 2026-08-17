import unittest
from pathlib import Path
from unittest.mock import patch

from collect_studio import avf_capture
from collect_studio import cams as cams_module


class FakeDevice:
    def __init__(self, supported=(0, 2), lock_ok=True):
        self.supported = set(supported)
        self.lock_ok = lock_ok
        self.mode = 0
        self.unlocks = 0
        self.locked = False

    def isExposureModeSupported_(self, mode):
        return mode in self.supported

    def lockForConfiguration_(self, _err):
        if not self.lock_ok:
            return False, "busy"
        self.locked = True
        return True, None

    def unlockForConfiguration(self):
        self.unlocks += 1
        self.locked = False

    def setExposureMode_(self, mode):
        if not self.locked:
            raise RuntimeError("not locked")
        if mode not in self.supported:
            raise RuntimeError("unsupported")
        self.mode = mode


class ApplyAutoExposureTest(unittest.TestCase):
    def test_default_disables_auto_exposure(self):
        from collect_studio.config_store import DEFAULTS

        self.assertFalse(DEFAULTS["record"]["auto_exposure"])

    def test_lock_mode_when_auto_exposure_off(self):
        dev = FakeDevice()
        result = avf_capture.apply_auto_exposure("cam-1", False, device=dev)
        self.assertTrue(result["ok"])
        self.assertEqual(result["mode"], "locked")
        self.assertEqual(dev.mode, avf_capture.AVF.AVCaptureExposureModeLocked)
        self.assertEqual(dev.unlocks, 1)

    def test_continuous_mode_when_auto_exposure_on(self):
        dev = FakeDevice()
        result = avf_capture.apply_auto_exposure("cam-1", True, device=dev)
        self.assertTrue(result["ok"])
        self.assertEqual(result["mode"], "continuous")
        self.assertEqual(dev.mode, avf_capture.AVF.AVCaptureExposureModeContinuousAutoExposure)

    def test_unsupported_mode_does_not_raise(self):
        dev = FakeDevice(supported=())
        result = avf_capture.apply_auto_exposure("cam-1", False, device=dev)
        self.assertFalse(result["ok"])
        self.assertIn("不支持", result["err"])
        self.assertEqual(dev.unlocks, 0)

    def test_missing_device(self):
        result = avf_capture.apply_auto_exposure("missing-camera-id", False)
        self.assertFalse(result["ok"])
        self.assertIn("不存在", result["err"])


class FakeStream:
    def __init__(self, unique_id, width, height, fps, auto_exposure=False):
        self.unique_id = unique_id
        self.width, self.height, self.fps = width, height, fps
        self.auto_exposure = auto_exposure
        self.ok = True
        self.err = None
        self.started_at = 0
        self.stopped = False

    def stop(self):
        self.stopped = True

    def set_auto_exposure(self, enabled):
        self.auto_exposure = bool(enabled)
        return self.exposure_status()

    def exposure_status(self):
        return {
            "auto_exposure": self.auto_exposure,
            "ok": True,
            "mode": "continuous" if self.auto_exposure else "locked",
            "err": None,
        }


CAMERA_CONFIG = {
    "cameras": {"wrist": "cam-1", "left_rear": "cam-2", "right_rear": "cam-3"},
    "record": {"fps": 30, "width": 640, "height": 480, "auto_exposure": False},
}
DEVICES = [
    {"index": i, "unique_id": f"cam-{i + 1}", "name": f"Camera {i + 1}", "builtin": False}
    for i in range(3)
]


class CameraManagerExposureTest(unittest.TestCase):
    def test_start_all_passes_default_auto_exposure_off(self):
        with (
            patch.object(cams_module, "enumerate_cameras", return_value=[dict(d) for d in DEVICES]),
            patch.object(cams_module, "CamStream", FakeStream),
            patch.object(cams_module.config_store, "load", return_value=CAMERA_CONFIG),
        ):
            manager = cams_module.CamManager()
            manager.start_all()
            self.assertEqual(len(manager.streams), 3)
            for stream in manager.streams.values():
                self.assertFalse(stream.auto_exposure)
            status = manager.status()
            self.assertFalse(status["auto_exposure"])
            self.assertEqual(status["exposure"]["cam-1"]["mode"], "locked")

    def test_set_auto_exposure_persists_and_updates_streams(self):
        saved = {}

        def fake_update(section, values):
            saved["section"] = section
            saved["values"] = dict(values)
            cfg[section] = dict(values)
            return cfg

        cfg = {
            "cameras": dict(CAMERA_CONFIG["cameras"]),
            "record": dict(CAMERA_CONFIG["record"]),
        }

        def fake_load():
            return cfg

        with (
            patch.object(cams_module, "enumerate_cameras", return_value=[dict(d) for d in DEVICES]),
            patch.object(cams_module, "CamStream", FakeStream),
            patch.object(cams_module.config_store, "load", side_effect=fake_load),
            patch.object(cams_module.config_store, "update", side_effect=fake_update),
            patch.object(
                cams_module.uvc_controls,
                "set_controls",
                return_value={"ok": False, "err": "mock", "controls": {}},
            ),
        ):
            manager = cams_module.CamManager()
            manager.start_all()
            status = manager.set_auto_exposure(True)
            self.assertEqual(saved["section"], "record")
            self.assertTrue(saved["values"]["auto_exposure"])
            self.assertTrue(status["auto_exposure"])
            for stream in manager.streams.values():
                self.assertTrue(stream.auto_exposure)
            self.assertEqual(status["exposure"]["cam-2"]["mode"], "continuous")


class FrontendExposureToggleTest(unittest.TestCase):
    def test_binding_page_has_per_camera_controls(self):
        html = (Path(__file__).parents[1] / "static" / "index.html").read_text()
        self.assertIn("/api/cams/controls/", html)
        self.assertIn("cam-ctrls", html)
        self.assertIn("自动曝光", html)
        self.assertIn("自动白平衡", html)
        self.assertNotIn('id="ae-toggle"', html)


if __name__ == "__main__":
    unittest.main()
