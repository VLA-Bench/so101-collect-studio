"""Central paths for collect-studio."""
from pathlib import Path

APP_ROOT = Path(__file__).resolve().parent.parent
ASSETS = APP_ROOT / "assets"
STATIC = APP_ROOT / "static"
CALIB_BACKUP = APP_ROOT / "configs" / "calibration_backup"
DEVICES_YAML = APP_ROOT / "configs" / "devices.yaml"
URDF = ASSETS / "so101_new_calib.urdf"

DATA_ROOT = Path.home() / "so101_data"
STAGING = DATA_ROOT / "staging"
LIBRARY = DATA_ROOT / "library"
TRASH = DATA_ROOT / "trash"
EXPORTS = DATA_ROOT / "exports"
TASKS_JSON = DATA_ROOT / "tasks.json"
COUNTER_JSON = DATA_ROOT / "counter.json"

LEROBOT_CALIB = Path.home() / ".cache" / "huggingface" / "lerobot" / "calibration"

for d in (STAGING, LIBRARY, TRASH, EXPORTS):
    d.mkdir(parents=True, exist_ok=True)
