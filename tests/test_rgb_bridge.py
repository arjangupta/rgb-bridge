import importlib.util
import os
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = REPO_ROOT / "src" / "rgb_bridge_daemon.py"
MONITOR_PATH = REPO_ROOT / "src" / "rgb_workload_monitor.py"

os.environ.setdefault("RGB_BRIDGE_ALLOWED_UID", "1000")
os.environ.setdefault("RGB_BRIDGE_SOCKET_GROUP", "users")

SPEC = importlib.util.spec_from_file_location("rgb_bridge_daemon", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
BROKER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(BROKER)

MONITOR_SPEC = importlib.util.spec_from_file_location("rgb_workload_monitor", MONITOR_PATH)
assert MONITOR_SPEC is not None and MONITOR_SPEC.loader is not None
MONITOR = importlib.util.module_from_spec(MONITOR_SPEC)
MONITOR_SPEC.loader.exec_module(MONITOR)


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

    def test_workload_uses_fixed_modes_and_zones(self) -> None:
        workload = {
            "cpu": {"color": "00FF00", "speed": 15},
            "gpu": {"color": "FFFF00", "speed": 50},
            "ram": {"color": "FF0000", "speed": 100},
        }
        args = BROKER.openrgb_args("workload", workload=workload)
        self.assertEqual(args.count("Color Pulse"), 2)
        self.assertEqual(args.count("Breathing"), 1 + len(BROKER.MOTHERBOARD_ZONES))
        self.assertIn(str(BROKER.COOLER_LEDS), args)
        self.assertEqual(args.count("FF0000,2E0000"), 2)

    def test_ram_dim_color(self) -> None:
        self.assertEqual(BROKER.dim_color("FF0000", 18), "2E0000")
        self.assertEqual(BROKER.dim_color("00FF00", 0), "000000")

    def test_workload_rejects_out_of_range_speed(self) -> None:
        response = BROKER.run_action(
            {
                "action": "workload",
                "cpu": {"color": "00FF00", "speed": 101},
                "gpu": {"color": "00FF00", "speed": 15},
                "ram": {"color": "00FF00", "speed": 15},
            },
            1000,
        )
        self.assertFalse(response["ok"])

    def test_commands_reject_extra_fields(self) -> None:
        response = BROKER.run_action({"action": "off", "extra": "ignored?"}, 1000)
        self.assertFalse(response["ok"])

    def test_known_openrgb_error_text_is_detected(self) -> None:
        output = "Wrong number of colors specified for mode Color Pulse\n"
        self.assertIsNotNone(BROKER.OPENRGB_ERROR_RE.search(output))


class WorkloadModelTests(unittest.TestCase):
    def test_color_thresholds(self) -> None:
        self.assertEqual(MONITOR.load_color(0), "00FF00")
        self.assertEqual(MONITOR.load_color(25), "00FF00")
        self.assertEqual(MONITOR.load_color(62.5), "FFFF00")
        self.assertEqual(MONITOR.load_color(100), "FF0000")

    def test_trend_speed_bounds(self) -> None:
        self.assertEqual(MONITOR.trend_speed(-100), 5)
        self.assertEqual(MONITOR.trend_speed(0), 15)
        self.assertEqual(MONITOR.trend_speed(100), 100)

    def test_rising_history_accelerates_breathing(self) -> None:
        history = MONITOR.TrendHistory()
        self.assertEqual(history.update(10, 0), 0)
        trend = history.update(80, 15)
        self.assertGreater(trend, 0)
        self.assertGreater(MONITOR.trend_speed(trend), 15)

    def test_ram_has_faster_minimum_speed(self) -> None:
        histories = {channel: MONITOR.TrendHistory() for channel in ("cpu", "gpu", "ram")}
        payload = MONITOR.build_payload({"cpu": 10, "gpu": 10, "ram": 10}, histories, 0)
        self.assertEqual(payload["cpu"]["speed"], 15)
        self.assertEqual(payload["ram"]["speed"], MONITOR.RAM_MIN_SPEED)


if __name__ == "__main__":
    unittest.main()
