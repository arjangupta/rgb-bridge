#!/usr/bin/python3
"""Map CPU, GPU, and memory pressure to RGB colors and breathing speeds."""

import argparse
import colorsys
import glob
import json
import math
import os
import re
import socket
import subprocess
import sys
import time
from dataclasses import dataclass, field

SOCKET_PATH = os.environ.get("RGB_BRIDGE_SOCKET", "/run/rgb-bridge/control.sock")
DEFAULT_INTERVAL = float(os.environ.get("RGB_BRIDGE_MONITOR_INTERVAL", "15"))
RAM_MIN_SPEED = int(os.environ.get("RGB_BRIDGE_RAM_MIN_SPEED", "55"))
WINDOWS = (60.0, 300.0, 900.0)


def clamp(value: float, low: float = 0.0, high: float = 100.0) -> float:
    return max(low, min(high, value))


def load_color(load: float) -> str:
    """Green through 25%, then smoothly transition through yellow to red."""
    load = clamp(load)
    if load <= 25.0:
        hue_degrees = 120.0
    else:
        hue_degrees = 120.0 * (1.0 - (load - 25.0) / 75.0)
    red, green, blue = colorsys.hsv_to_rgb(hue_degrees / 360.0, 1.0, 1.0)
    return f"{round(red * 255):02X}{round(green * 255):02X}{round(blue * 255):02X}"


def trend_speed(trend: float) -> int:
    """Map falling/stable/rising percentage-point trends to breathing speed."""
    if trend <= 0.0:
        return round(clamp(15.0 + trend * 2.0, 5.0, 15.0))
    return round(clamp(15.0 + trend * 4.25, 15.0, 100.0))


@dataclass
class TrendHistory:
    averages: dict[float, float] = field(default_factory=dict)
    last_time: float | None = None

    def update(self, value: float, now: float) -> float:
        value = clamp(value)
        if self.last_time is None:
            self.averages = {window: value for window in WINDOWS}
            self.last_time = now
            return 0.0

        elapsed = max(0.001, now - self.last_time)
        self.last_time = now
        for window in WINDOWS:
            alpha = 1.0 - math.exp(-elapsed / window)
            self.averages[window] += alpha * (value - self.averages[window])

        one = self.averages[60.0]
        five = self.averages[300.0]
        fifteen = self.averages[900.0]
        return 0.55 * (value - one) + 0.30 * (one - five) + 0.15 * (five - fifteen)


class Telemetry:
    def __init__(self) -> None:
        self.previous_cpu: tuple[int, int] | None = None
        self.last_gpu = 0.0
        self.warned_gpu = False

    @staticmethod
    def _cpu_counters() -> tuple[int, int]:
        fields = [int(value) for value in open("/proc/stat", encoding="utf-8").readline().split()[1:]]
        total = sum(fields)
        idle = fields[3] + (fields[4] if len(fields) > 4 else 0)
        return total, idle

    def cpu(self) -> float:
        current = self._cpu_counters()
        if self.previous_cpu is None:
            self.previous_cpu = current
            return clamp(os.getloadavg()[0] * 100.0 / (os.cpu_count() or 1))
        total_delta = current[0] - self.previous_cpu[0]
        idle_delta = current[1] - self.previous_cpu[1]
        self.previous_cpu = current
        if total_delta <= 0:
            return 0.0
        return clamp(100.0 * (total_delta - idle_delta) / total_delta)

    @staticmethod
    def memory() -> float:
        values: dict[str, int] = {}
        with open("/proc/meminfo", encoding="utf-8") as meminfo:
            for line in meminfo:
                key, raw = line.split(":", 1)
                if key in {"MemTotal", "MemAvailable"}:
                    values[key] = int(raw.split()[0])
        total = values["MemTotal"]
        return clamp(100.0 * (total - values["MemAvailable"]) / total)

    @staticmethod
    def _gpu_sysfs() -> float | None:
        for path in glob.glob("/sys/class/drm/card*/device/gpu_busy_percent"):
            try:
                return clamp(float(open(path, encoding="utf-8").read().strip()))
            except (OSError, ValueError):
                continue
        return None

    @staticmethod
    def _run(command: list[str], env: dict[str, str] | None = None) -> str | None:
        try:
            result = subprocess.run(
                command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                timeout=3,
                check=False,
                env=env,
            )
        except (OSError, subprocess.TimeoutExpired):
            return None
        return result.stdout.strip() or None

    @classmethod
    def _desktop_environment(cls) -> dict[str, str]:
        env = os.environ.copy()
        runtime_dir = env.setdefault("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
        env.setdefault("DBUS_SESSION_BUS_ADDRESS", f"unix:path={runtime_dir}/bus")
        output = cls._run(["systemctl", "--user", "show-environment"], env=env)
        for line in (output or "").splitlines():
            key, separator, value = line.partition("=")
            if separator and key in {"DISPLAY", "XAUTHORITY"}:
                env[key] = value
        authority = f"{runtime_dir}/gdm/Xauthority"
        if "XAUTHORITY" not in env and os.path.isfile(authority):
            env["XAUTHORITY"] = authority
        return env

    @classmethod
    def _gpu_nvidia_settings(cls) -> float | None:
        env = cls._desktop_environment()
        displays = []
        if env.get("DISPLAY"):
            displays.append(env["DISPLAY"])
        displays += [display for display in (":0", ":1") if display not in displays]
        for display in displays:
            output = cls._run(
                ["nvidia-settings", "-c", display, "-q", "GPUUtilization", "-t"],
                env=env,
            )
            match = re.search(r"graphics=(\d+)", output or "")
            if match:
                return clamp(float(match.group(1)))
        return None

    def gpu(self) -> float:
        value = self._gpu_sysfs()
        if value is None:
            output = self._run([
                "nvidia-smi",
                "--query-gpu=utilization.gpu",
                "--format=csv,noheader,nounits",
            ])
            if output:
                try:
                    value = clamp(float(output.splitlines()[0].strip()))
                except ValueError:
                    value = None
        if value is None:
            value = self._gpu_nvidia_settings()

        if value is None:
            if not self.warned_gpu:
                print("GPU utilization unavailable; retaining the last value", file=sys.stderr, flush=True)
                self.warned_gpu = True
            return self.last_gpu

        self.warned_gpu = False
        self.last_gpu = value
        return value


def broker_request(payload: dict) -> dict:
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(40)
        client.connect(SOCKET_PATH)
        client.sendall((json.dumps(payload) + "\n").encode("utf-8"))
        client.shutdown(socket.SHUT_WR)
        response = bytearray()
        while True:
            chunk = client.recv(4096)
            if not chunk:
                break
            response.extend(chunk)
    return json.loads(response.decode("utf-8"))


def build_payload(values: dict[str, float], histories: dict[str, TrendHistory], now: float) -> dict:
    payload: dict = {"action": "workload"}
    for channel, value in values.items():
        trend = histories[channel].update(value, now)
        speed = trend_speed(trend)
        if channel == "ram":
            speed = max(RAM_MIN_SPEED, speed)
        payload[channel] = {
            "color": load_color(value),
            "speed": speed,
        }
    return payload


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--interval", type=float, default=DEFAULT_INTERVAL)
    parser.add_argument("--once", action="store_true", help="sample and apply once")
    parser.add_argument("--dry-run", action="store_true", help="print samples without contacting the broker")
    args = parser.parse_args()
    if args.interval < 5.0:
        parser.error("--interval must be at least 5 seconds")

    telemetry = Telemetry()
    histories = {channel: TrendHistory() for channel in ("cpu", "gpu", "ram")}

    while True:
        started = time.monotonic()
        values = {
            "cpu": telemetry.cpu(),
            "gpu": telemetry.gpu(),
            "ram": telemetry.memory(),
        }
        payload = build_payload(values, histories, started)
        report = {
            channel: {
                "load": round(values[channel], 1),
                **payload[channel],
            }
            for channel in ("cpu", "gpu", "ram")
        }
        print(json.dumps(report, sort_keys=True), flush=True)

        if not args.dry_run:
            try:
                response = broker_request(payload)
                if not response.get("ok"):
                    print(response.get("error", "workload RGB update failed"), file=sys.stderr, flush=True)
            except (OSError, json.JSONDecodeError) as exc:
                print(f"RGB broker request failed: {exc}", file=sys.stderr, flush=True)

        if args.once:
            return 0
        remaining = args.interval - (time.monotonic() - started)
        if remaining > 0:
            time.sleep(remaining)


if __name__ == "__main__":
    raise SystemExit(main())
