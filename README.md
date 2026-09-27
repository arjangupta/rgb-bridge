# RGB Bridge

A deliberately small privilege boundary around [OpenRGB](https://openrgb.org/).
It lets one local desktop user change a known set of RGB devices without running
their shell, desktop session, or automation agent as root.

This repository captures the working configuration for:

- two Corsair Vengeance RGB Pro DDR4 modules (OpenRGB devices 0 and 1)
- a Gigabyte Z390 AORUS PRO WIFI motherboard (OpenRGB device 2)
- a Zalman CNPS20X cooler connected to motherboard header `D_LED1`

The Zalman fans share the header through their included Y-cable. Each SF140 fan
has ten physical LEDs. On this Gigabyte IT8297 controller, built-in motherboard
effects reliably address the complete fans; OpenRGB Direct mode only updates
part of their internal buffer. Consequently, normal `cycle`, `static`, and
`off` actions use motherboard hardware effects. `cooler-test` is retained only
as a diagnostic for the two digital headers.

## Requirements

- Linux with systemd
- Python 3.10 or newer
- OpenRGB installed at `/opt/openrgb/AppRun`
- the target devices visible to OpenRGB when it runs as root

OpenRGB itself is intentionally not committed here. Install or update it from
the official project before installing the bridge.

## Install

Run the installer from the desktop account that should control RGB:

```sh
sudo ./install
```

The installer records the invoking `sudo` user's numeric UID and primary group
in `/etc/default/rgb-bridge`, installs root-owned executables and systemd units,
starts the broker, tests the cycle action, and enables cycling at boot. It also
installs a system-sleep hook that reapplies the cycle after resume.

To explicitly select another local account:

```sh
sudo RGB_BRIDGE_USER=alice ./install
```

## Commands

All normal commands are unprivileged after installation:

```sh
rgbctl cycle
rgbctl static FF6600
rgbctl off
rgbctl list
rgbctl status
```

Diagnostic header probes are deliberately constrained to header 1 or 2 and a
six-digit color:

```sh
rgbctl cooler-test 1 FF0000
```

## Configuration

The installer creates `/etc/default/rgb-bridge`. These defaults match the
machine for which this bridge was built:

```sh
RGB_BRIDGE_RAM_DEVICES=0,1
RGB_BRIDGE_MOTHERBOARD_DEVICE=2
RGB_BRIDGE_COOLER_LEDS=10
```

OpenRGB device indices can change when hardware changes. Check them with
`rgbctl list`, update the root-owned environment file, and restart
`rgb-bridge.service` if necessary.

## Security model

- The daemon runs as root because motherboard and SMBus access require it.
- It accepts requests only through `/run/rgb-bridge/control.sock`.
- The socket is mode `0660`, owned by root and the configured user's primary
  group; its parent directory is mode `0750`.
- Linux `SO_PEERCRED` must report either root or the configured numeric UID.
- Requests are JSON limited to 4096 bytes.
- Only `cycle`, `static`, `off`, `cooler-test`, `list`, and `status` exist.
- Colors must be exactly six hexadecimal digits; header probes accept only 1
  or 2. Callers cannot supply paths, device indices, modes, or extra OpenRGB
  arguments.
- The systemd unit uses filesystem protections and grants writes only to the
  runtime and state directories.

## Development checks

```sh
python3 -m unittest discover -s tests -v
python3 -m py_compile src/rgb_bridge_daemon.py bin/rgbctl
```

## Uninstall

```sh
sudo systemctl disable --now rgb-cycle.service rgb-bridge.service
sudo rm -f /etc/systemd/system/rgb-cycle.service \
  /etc/systemd/system/rgb-bridge.service \
  /usr/lib/systemd/system-sleep/rgb-cycle \
  /usr/local/bin/rgbctl \
  /usr/local/libexec/rgb-bridge-daemon \
  /etc/default/rgb-bridge
sudo systemctl daemon-reload
```

The state directory `/var/lib/rgb-bridge` is intentionally left in place so
removal does not destroy configuration implicitly.
