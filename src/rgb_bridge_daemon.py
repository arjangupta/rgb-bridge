#!/usr/bin/python3
"""Narrow privileged broker for local OpenRGB lighting actions."""

import grp
import json
import os
import re
import signal
import socket
import struct
import subprocess
import sys
from pathlib import Path

SOCKET_PATH = Path(os.environ.get("RGB_BRIDGE_SOCKET", "/run/rgb-bridge/control.sock"))
STATE_PATH = Path(os.environ.get("RGB_BRIDGE_STATE", "/var/lib/rgb-bridge/last-action.json"))
OPENRGB = os.environ.get("RGB_BRIDGE_OPENRGB", "/opt/openrgb/AppRun")
CONFIG_DIR = os.environ.get("RGB_BRIDGE_OPENRGB_CONFIG", "/var/lib/rgb-bridge/openrgb")
ALLOWED_UID = int(os.environ.get("RGB_BRIDGE_ALLOWED_UID", "-1"))
SOCKET_GROUP = os.environ.get("RGB_BRIDGE_SOCKET_GROUP", "")
RAM_DEVICES = tuple(
    int(value) for value in os.environ.get("RGB_BRIDGE_RAM_DEVICES", "0,1").split(",")
)
MOTHERBOARD_DEVICE = int(os.environ.get("RGB_BRIDGE_MOTHERBOARD_DEVICE", "2"))
COOLER_LEDS = int(os.environ.get("RGB_BRIDGE_COOLER_LEDS", "10"))
COOLER_ZONE = int(os.environ.get("RGB_BRIDGE_COOLER_ZONE", "0"))
MOTHERBOARD_ZONES = tuple(
    int(value)
    for value in os.environ.get("RGB_BRIDGE_MOTHERBOARD_ZONES", "2,3,4,5,6,7").split(",")
)
RAM_DIM_PERCENT = int(os.environ.get("RGB_BRIDGE_RAM_DIM_PERCENT", "18"))
MAX_REQUEST = 4096
COLOR_RE = re.compile(r"^[0-9A-Fa-f]{6}$")
OPENRGB_ERROR_RE = re.compile(
    r"^(?:Wrong number of colors specified|Please provide between|Invalid |Unknown )",
    re.MULTILINE,
)


def dim_color(color: str, percent: int = RAM_DIM_PERCENT) -> str:
    factor = max(0, min(100, percent)) / 100.0
    channels = [int(color[index:index + 2], 16) for index in (0, 2, 4)]
    return "".join(f"{round(channel * factor):02X}" for channel in channels)


def openrgb_args(
    action: str,
    color: str | None = None,
    cooler_header: int | None = None,
    workload: dict | None = None,
) -> list[str]:
    base = [OPENRGB, "--noautoconnect", "--config", CONFIG_DIR]
    motherboard = str(MOTHERBOARD_DEVICE)
    if action == "cycle":
        args = list(base)
        for device in RAM_DEVICES:
            args += [
                "--device", str(device), "--mode", "Rainbow Wave", "--speed", "60",
            ]
        return args + [
            "--device", motherboard, "--mode", "Color Cycle", "--speed", "60",
        ]
    if action in {"static", "off"}:
        selected_color = "000000" if action == "off" else color
        assert selected_color is not None
        args = list(base)
        for device in RAM_DEVICES:
            args += [
                "--device", str(device), "--mode", "Direct", "--color", selected_color,
            ]
        return args + [
            "--device", motherboard, "--mode", "Static", "--color", selected_color,
        ]
    if action == "cooler-test":
        assert color is not None
        assert cooler_header in {1, 2}
        return base + [
            "--device", motherboard,
            "--zone", str(cooler_header - 1),
            "--size", str(COOLER_LEDS),
            "--mode", "Direct",
            "--color", color,
        ]

    if action == "workload":
        assert workload is not None
        args = list(base)
        ram = workload["ram"]
        cpu = workload["cpu"]
        gpu = workload["gpu"]
        for device in RAM_DEVICES:
            args += [
                "--device", str(device),
                "--mode", "Color Pulse",
                "--speed", str(ram["speed"]),
                "--color", f"{ram['color']},{dim_color(ram['color'])}",
            ]
        args += [
            "--device", motherboard,
            "--zone", str(COOLER_ZONE),
            "--size", str(COOLER_LEDS),
            "--mode", "Breathing",
            "--speed", str(cpu["speed"]),
            "--color", cpu["color"],
        ]
        for zone in MOTHERBOARD_ZONES:
            args += [
                "--device", motherboard,
                "--zone", str(zone),
                "--mode", "Breathing",
                "--speed", str(gpu["speed"]),
                "--color", gpu["color"],
            ]
        return args
    if action == "list":
        return base + ["--list-detailed"]
    raise ValueError("unsupported action")


def run_action(request: dict, uid: int) -> dict:
    action = request.get("action")
    if action == "status":
        if set(request) != {"action"}:
            return {"ok": False, "error": "status accepts no additional fields"}
        try:
            last = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            last = None
        return {"ok": True, "service": "ready", "last_action": last}

    if action not in {"cycle", "static", "off", "cooler-test", "workload", "list"}:
        return {
            "ok": False,
            "error": "action must be cycle, static, off, cooler-test, workload, list, or status",
        }

    expected_fields = {
        "cycle": {"action"},
        "static": {"action", "color"},
        "off": {"action"},
        "cooler-test": {"action", "header", "color"},
        "workload": {"action", "cpu", "gpu", "ram"},
        "list": {"action"},
    }[action]
    if set(request) != expected_fields:
        return {"ok": False, "error": f"{action} request fields are invalid"}

    color = request.get("color")
    if action in {"static", "cooler-test"}:
        if not isinstance(color, str) or not COLOR_RE.fullmatch(color):
            return {"ok": False, "error": "color must be exactly six hexadecimal digits"}
        color = color.upper()
    elif color is not None:
        return {"ok": False, "error": "color is only valid with static or cooler-test"}

    cooler_header = request.get("header")
    if action == "cooler-test":
        if not isinstance(cooler_header, int) or cooler_header not in {1, 2}:
            return {"ok": False, "error": "header must be 1 or 2"}
    elif cooler_header is not None:
        return {"ok": False, "error": "header is only valid with cooler-test"}

    workload = None
    if action == "workload":
        workload = {}
        for channel in ("cpu", "gpu", "ram"):
            value = request.get(channel)
            if not isinstance(value, dict) or set(value) != {"color", "speed"}:
                return {"ok": False, "error": f"{channel} must contain color and speed"}
            channel_color = value.get("color")
            channel_speed = value.get("speed")
            if not isinstance(channel_color, str) or not COLOR_RE.fullmatch(channel_color):
                return {"ok": False, "error": f"{channel} color must be six hexadecimal digits"}
            if isinstance(channel_speed, bool) or not isinstance(channel_speed, int) or not 0 <= channel_speed <= 100:
                return {"ok": False, "error": f"{channel} speed must be an integer from 0 to 100"}
            workload[channel] = {
                "color": channel_color.upper(),
                "speed": channel_speed,
            }

    command = openrgb_args(action, color, cooler_header, workload)
    try:
        result = subprocess.run(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=30,
            check=False,
            env={"HOME": "/var/lib/rgb-bridge", "PATH": "/usr/sbin:/usr/bin:/sbin:/bin"},
        )
    except subprocess.TimeoutExpired as exc:
        return {"ok": False, "error": "OpenRGB timed out", "output": exc.stdout or ""}

    output_has_error = bool(OPENRGB_ERROR_RE.search(result.stdout))
    response = {
        "ok": result.returncode == 0 and not output_has_error,
        "exit_code": result.returncode,
        "output": result.stdout[-16000:],
    }
    if result.returncode != 0 or output_has_error:
        response["error"] = (
            f"OpenRGB exited with code {result.returncode}"
            if result.returncode != 0
            else "OpenRGB reported a command error"
        )
        print(
            f"OpenRGB action={action} failed exit={result.returncode}: {result.stdout[-2000:]}",
            file=sys.stderr,
            flush=True,
        )
    if response["ok"] and action != "list":
        record = {"action": action, "requested_by_uid": uid}
        if color is not None:
            record["color"] = color
        if cooler_header is not None:
            record["header"] = cooler_header
        if workload is not None:
            record["channels"] = workload
        STATE_PATH.write_text(json.dumps(record, sort_keys=True) + "\n", encoding="utf-8")
        os.chmod(STATE_PATH, 0o600)
    return response


def receive_request(connection: socket.socket) -> bytes:
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = connection.recv(min(1024, MAX_REQUEST + 1 - total))
        if not chunk:
            break
        chunks.append(chunk)
        total += len(chunk)
        if total > MAX_REQUEST:
            raise ValueError("request too large")
        if b"\n" in chunk:
            break
    return b"".join(chunks).split(b"\n", 1)[0]


def main() -> int:
    if os.geteuid() != 0:
        print("rgb-bridge-daemon must run as root", file=sys.stderr)
        return 1
    if ALLOWED_UID <= 0 or not SOCKET_GROUP:
        print("RGB_BRIDGE_ALLOWED_UID and RGB_BRIDGE_SOCKET_GROUP must be configured", file=sys.stderr)
        return 1

    try:
        socket_gid = grp.getgrnam(SOCKET_GROUP).gr_gid
    except KeyError:
        print(f"configured socket group does not exist: {SOCKET_GROUP}", file=sys.stderr)
        return 1

    Path(CONFIG_DIR).mkdir(parents=True, exist_ok=True)
    SOCKET_PATH.parent.mkdir(parents=True, exist_ok=True)
    os.chown(SOCKET_PATH.parent, 0, socket_gid)
    os.chmod(SOCKET_PATH.parent, 0o750)
    try:
        SOCKET_PATH.unlink()
    except FileNotFoundError:
        pass

    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(str(SOCKET_PATH))
    os.chown(SOCKET_PATH, 0, socket_gid)
    os.chmod(SOCKET_PATH, 0o660)
    server.listen(8)

    def stop(_signum: int, _frame: object) -> None:
        server.close()
        try:
            SOCKET_PATH.unlink()
        except FileNotFoundError:
            pass
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)

    while True:
        try:
            connection, _ = server.accept()
        except OSError:
            return 0
        with connection:
            try:
                pid, uid, _gid = struct.unpack("3i", connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
                if uid not in {0, ALLOWED_UID}:
                    response = {"ok": False, "error": "caller is not authorized"}
                else:
                    raw = receive_request(connection)
                    request = json.loads(raw.decode("utf-8"))
                    if not isinstance(request, dict):
                        raise ValueError("request must be an object")
                    print(f"request pid={pid} uid={uid} action={request.get('action')}", flush=True)
                    response = run_action(request, uid)
            except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
                response = {"ok": False, "error": str(exc)}
            try:
                connection.sendall((json.dumps(response) + "\n").encode("utf-8"))
            except BrokenPipeError:
                pass


if __name__ == "__main__":
    raise SystemExit(main())
