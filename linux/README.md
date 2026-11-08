# Pianblox on Linux

A Linux port of the Roblox piano player, for Wayland (built and tested on Hyprland, Debian 13) with
[Sober](https://sober.vinegarhq.org/) as the Roblox client. Same `.sched` songs, same key layout and the same
timing rules as the Windows version; only the plumbing changes.

| Windows | Linux |
|---|---|
| `keybd_event` (`user32.dll`) | a virtual keyboard through `/dev/uinput` (python-evdev) |
| `GetForegroundWindow` title contains "roblox" | Hyprland IPC: the focused window's **class** matches `sober\|roblox` |
| global hotkeys END / F8 / Ctrl+C | **Backspace** / **Space** in the game (a Hyprland submap the player sets up itself), Ctrl+C, or `pianblox ctl stop\|pause` |
| C# engine compiled by PowerShell | a single Python file with an allocation-light loop |

## Setup (once)

```bash
./linux/install.sh        # asks for sudo once
```

It installs `python3-evdev` and `python3-mido`, adds a udev rule that lets the logged-in desktop user create a
virtual keyboard (`TAG+="uaccess"` on `/dev/uinput`, no logout needed), and links `pianblox` into `~/.local/bin`.

## Playing

```bash
pianblox                       # menu
pianblox --song canon          # skip the menu
pianblox --song canon --speed 0.9
pianblox --song canon --dry-run
pianblox --self-test           # strike all 61 keys one at a time
```

Open Sober and a Roblox piano first. During the countdown click inside the game so it has the keyboard. Leave the
game's own transpose at 0.

While playing, with the game in front (both lift every held key):

| Key | |
|---|---|
| **Space** | pause / resume (the schedule slides by the pause length) |
| **Backspace** | stop |
| focus leaves Sober | pauses by itself and resumes when Sober is back in front |

Nothing to configure: when the song starts the player puts Hyprland into its own submap `pianblox`, and leaves it when
the song ends or Sober loses focus. Why a submap and not a plain `SUPER + End` keybind:

- The player holds Shift for every black key, and Hyprland adds up the modifiers of every keyboard, so `SUPER + End`
  arrived as `SUPER + SHIFT + End` about half the time and nothing happened. The submap binds use `ignore_mods`.
- On many laptops (this was an ASUS Vivobook Go) F8 and End only exist behind Fn. Space and Backspace are on every
  keyboard.
- Inside the submap every other shortcut is off, so a Super you happen to hold cannot combine with a letter the
  player strikes into something like `SUPER + Q`. Click another window to get them back (the song pauses).

Outside the game, `pianblox ctl pause` / `pianblox ctl stop` still work, and Ctrl+C in the terminal stops. If the
player is ever killed hard (`kill -9`) and the keyboard seems stuck without shortcuts, press Space or Backspace once:
with no player running, that just switches Hyprland back to its normal keymap.

## Adding a song

```bash
pianblox add song.mid --list                                   # what is inside the file
pianblox add song.mid --title "Song" --artist "Someone" --parts 1,3
```

Same rules as `Add-Song.ps1`: a part is one channel inside one track, drums are always dropped, a key must be up for
60 ms before it is struck again, a strike shorter than 130 ms is stretched, the same pitch doubled across tracks is
folded into one note, and when two different pitches want one physical key (`q` and `Q` are the same key) the higher
one wins. `--offset` slides the piece across the keyboard in semitones (default 36).

## What was verified (and what was not)

Measured on the development machine, with the virtual keyboard grabbed exclusively so nothing reached any window:

- Key order and Shift handling: a test schedule with white keys, black keys and a white+black chord produced exactly
  the expected 16 key events. Shift is held, not pulsed, and released with the right colour.
- Timing: a full 5:52 song at 12x speed (heavier than normal) finished with mean error 0.03 ms and worst 1.38 ms.
- Stop lifts every held key including Shift; pause lifts them and resumes with the schedule shifted by the pause.
- The focus check reads Hyprland's active window in about 0.3 ms.
- The MIDI converter was checked against a synthetic file covering doubled notes, a `q`/`Q` conflict, drums and a
  too-fast repeat.

- Hotkeys: with a second virtual keyboard holding Shift the whole time, Space paused, Space resumed and Backspace
  stopped, each within about 0.1 s, and the normal keymap came back afterwards.
- The virtual keyboard: Hyprland takes about 2 s to list a new keyboard, so the player waits for it to show up
  before the first key instead of sleeping a fixed 0.8 s.

**Not verified:** the notes actually sounding in a Roblox piano through Sober. If long notes stutter, suspect key
repeat: a held key may be auto-repeated by the compositor or the client. Raising `repeat_delay` in the Hyprland
`input` config is the first thing to try.

## Troubleshooting

- `No access to /dev/uinput`: run `./linux/install.sh`; if it still fails, log out and back in once.
- The song pauses forever with "Roblox lost focus": the game's window class is not matched. Check it with
  `hyprctl activewindow -j | grep class` and pass `--window-match <regex>`, or use `--no-focus-guard`.
- Nothing happens in the game: click inside the game during the countdown; the game must own the keyboard.
- This does not work on X11-only setups without `/dev/uinput` access; it needs the udev rule above.
