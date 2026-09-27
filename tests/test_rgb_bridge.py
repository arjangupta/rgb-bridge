import importlib.util
import os
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = REPO_ROOT / "src" / "rgb_bridge_daemon.py"

os.environ.setdefault("RGB_BRIDGE_ALLOWED_UID", "1000")
os.environ.setdefault("RGB_BRIDGE_SOCKET_GROUP", "users")

SPEC = importlib.util.spec_from_file_location("rgb_bridge_daemon", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
BROKER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(BROKER)


class OpenRGBCommandTests(unittest.TestCase):
    def test_cycle_uses_hardware_effect_for_motherboard(self) -> None:
        args = BROKER.openrgb_args("cycle")
        self.assertIn("Color Cycle", args)
        self.assertEqual(args.count("Rainbow Wave"), 2)

    def test_static_uses_hardware_static_for_motherboard(self) -> None:
        args = BROKER.openrgb_args("static", "A1B2C3")
        motherboard_position = args.index(str(BROKER.MOTHERBOARD_DEVICE))
        self.assertEqual(args[motherboard_position + 2], "Static")
        self.assertEqual(args[-1], "A1B2C3")

    def test_off_is_static_black(self) -> None:
        args = BROKER.openrgb_args("off")
        self.assertNotIn("Color Cycle", args)
        self.assertEqual(args.count("000000"), 3)

    def test_cooler_probe_is_constrained(self) -> None:
        args = BROKER.openrgb_args("cooler-test", "FF0000", 1)
        self.assertEqual(
            args[-8:],
            [
                "--zone", "0",
                "--size", str(BROKER.COOLER_LEDS),
                "--mode", "Direct",
                "--color", "FF0000",
            ],
        )


if __name__ == "__main__":
    unittest.main()
