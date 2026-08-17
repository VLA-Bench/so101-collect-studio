import unittest
from unittest.mock import patch

from collect_studio import cams as cams_module
from collect_studio import uvc_controls


class UniqueIdCodecTest(unittest.TestCase):
    def test_roundtrip_icspring_and_realtek(self):
        cases = [
            (0x01131000, 0x2993, 0x0858, "0x113100029930858"),
            (0x01132000, 0x0BDA, 0x1376, "0x11320000bda1376"),
            (0x01133000, 0x0BDA, 0x1376, "0x11330000bda1376"),
        ]
        for loc, vid, pid, uid in cases:
            self.assertEqual(uvc_controls.unique_id_from_usb(loc, vid, pid), uid)
            self.assertEqual(uvc_controls.location_id_from_unique_id(uid), loc)

    def test_builtin_uuid_is_not_uvc(self):
        self.assertIsNone(
            uvc_controls.location_id_from_unique_id("6C707041-05AC-0011-0004-000000000001")
        )


class AutoExposureCodecTest(unittest.TestCase):
    def test_manual_and_aperture_only(self):
        self.assertFalse(uvc_controls.decode_auto_exposure(1))
        self.assertTrue(uvc_controls.decode_auto_exposure(8))
        self.assertTrue(uvc_controls.decode_auto_exposure(2))
        self.assertEqual(uvc_controls.encode_auto_exposure(False), 1)
        self.assertEqual(uvc_controls.encode_auto_exposure(True), 8)


class NormalizeAndSnapshotTest(unittest.TestCase):
    def test_normalize_patch_keeps_known_keys(self):
        patch = uvc_controls.normalize_patch({
            "auto_exposure": 0,
            "brightness": "4",
            "unknown": 1,
            "gain": None,
        })
        self.assertEqual(patch, {"auto_exposure": False, "brightness": 4})

    def test_snapshot_to_saved_skips_unsupported(self):
        saved = uvc_controls.snapshot_to_saved({
            "controls": {
                "auto_exposure_mode": {"supported": True, "auto": False, "value": 1},
                "brightness": {"supported": True, "value": 4},
                "gain": {"supported": False, "value": None},
                "contrast": {"supported": True, "value": None},
            }
        })
        self.assertEqual(saved, {"auto_exposure": False, "brightness": 4})


class ApplyPatchOrderTest(unittest.TestCase):
    def test_turns_off_ae_before_writing_exposure(self):
        writes = []

        def xfer(bm, request, entity, selector, size, payload=None):
            if bm == uvc_controls.BM_SET:
                writes.append((entity, selector, payload))
            return 0, payload or b"\x00" * size

        uvc_controls._apply_patch(xfer, {
            "auto_exposure": False,
            "absolute_exposure_time": 75,
            "brightness": 4,
        })
        selectors = [item[1] for item in writes]
        self.assertEqual(selectors[0], 0x02)  # AE manual first
        self.assertIn(0x04, selectors)  # then exposure
        self.assertLess(selectors.index(0x02), selectors.index(0x04))
        self.assertEqual(writes[0][2], bytes([uvc_controls.AE_MANUAL]))


class FakeStream:
    def __init__(self, unique_id, width, height, fps, auto_exposure=False):
        self.unique_id = unique_id
        self.auto_exposure = auto_exposure
        self.ok = True
        self.err = None
        self.started_at = 0

    def stop(self):
        pass

    def set_auto_exposure(self, enabled):
        self.auto_exposure = bool(enabled)
        return {"ok": True, "mode": "continuous" if enabled else "locked", "err": None}

    def exposure_status(self):
        return {
            "auto_exposure": self.auto_exposure,
            "ok": True,
            "mode": "continuous" if self.auto_exposure else "locked",
            "err": None,
        }


class CameraManagerPerCamControlsTest(unittest.TestCase):
    def test_set_uvc_controls_persists_each_uid_independently(self):
        cfg = {
            "cameras": {"wrist": "0xaaa", "left_rear": "0xbbb", "right_rear": "0xccc"},
            "record": {"fps": 30, "width": 640, "height": 480, "auto_exposure": False},
            "camera_controls": {},
        }
        devices = [
            {"index": 0, "unique_id": "0xaaa", "name": "A", "builtin": False},
            {"index": 1, "unique_id": "0xbbb", "name": "B", "builtin": False},
        ]

        def fake_load():
            return cfg

        def fake_update(section, values):
            cfg.setdefault(section, {}).update(values)
            return cfg

        def fake_set(uid, values):
            return {
                "ok": True,
                "err": None,
                "unique_id": uid,
                "controls": {
                    "auto_exposure_mode": {
                        "supported": True,
                        "auto": bool(values.get("auto_exposure", False)),
                        "value": 8 if values.get("auto_exposure") else 1,
                    },
                    "brightness": {
                        "supported": True,
                        "value": values.get("brightness", 0),
                    },
                },
            }

        with (
            patch.object(cams_module, "enumerate_cameras", return_value=devices),
            patch.object(cams_module, "CamStream", FakeStream),
            patch.object(cams_module.config_store, "load", side_effect=fake_load),
            patch.object(cams_module.config_store, "update", side_effect=fake_update),
            patch.object(cams_module.uvc_controls, "set_controls", side_effect=fake_set),
        ):
            manager = cams_module.CamManager()
            manager.start_all()
            manager.set_uvc_controls("0xaaa", {"brightness": 10, "auto_exposure": False})
            manager.set_uvc_controls("0xbbb", {"brightness": -4, "auto_exposure": True})
            self.assertEqual(cfg["camera_controls"]["0xaaa"]["brightness"], 10)
            self.assertFalse(cfg["camera_controls"]["0xaaa"]["auto_exposure"])
            self.assertEqual(cfg["camera_controls"]["0xbbb"]["brightness"], -4)
            self.assertTrue(cfg["camera_controls"]["0xbbb"]["auto_exposure"])
            self.assertTrue(manager.streams["0xbbb"].auto_exposure)
            self.assertFalse(manager.streams["0xaaa"].auto_exposure)
            self.assertNotIn("0xccc", cfg["camera_controls"])


if __name__ == "__main__":
    unittest.main()
