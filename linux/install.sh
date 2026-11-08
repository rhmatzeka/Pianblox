#!/usr/bin/env bash
# One-time setup for the Linux version of Pianblox (Debian/Ubuntu + Wayland/Hyprland).
# Needs sudo once: it installs two Python packages and lets your desktop user create a virtual keyboard.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "[1/4] Python packages (python3-evdev, python3-mido)"
sudo apt-get install -y python3-evdev python3-mido

echo "[2/4] /dev/uinput access for the logged-in desktop user (udev 'uaccess' rule)"
sudo tee /etc/udev/rules.d/70-pianblox-uinput.rules >/dev/null <<'RULE'
# Pianblox: let the logged-in desktop user create a virtual keyboard via /dev/uinput.
KERNEL=="uinput", SUBSYSTEM=="misc", TAG+="uaccess", OPTIONS+="static_node=uinput"
RULE
echo uinput | sudo tee /etc/modules-load.d/uinput.conf >/dev/null
sudo modprobe uinput
sudo udevadm control --reload-rules
sudo udevadm trigger --name-match=uinput

echo "[3/4] 'pianblox' command"
mkdir -p "$HOME/.local/bin"
ln -sfn "$HERE/pianblox.py" "$HOME/.local/bin/pianblox"

echo "[4/4] check"
python3 - <<'PY'
import time
from evdev import UInput, ecodes as e
ui = UInput({e.EV_KEY: [e.KEY_A]}, name="pianblox-check")
time.sleep(0.2); ui.close()
print("  virtual keyboard: OK")
PY

cat <<'MSG'

Done. Try:   pianblox list
             pianblox --song canon --dry-run

While a song plays with the game in front: Space = pause/resume, Backspace = stop.
No keybinds to add; the player sets them up in Hyprland by itself.
MSG
