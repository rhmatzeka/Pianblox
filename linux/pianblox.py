#!/usr/bin/env python3
"""
Roblox virtual-piano autoplayer for Linux (Wayland / Hyprland).

Linux port of Pianblox (Play.ps1 + Play-Piano.ps1 + Engine.cs). Same schedule
format (.sched), same key layout, same rules: Shift is held rather than pulsed,
a key is lifted with the same Shift state it was struck with, and every instant
is timed from the start so a hiccup corrects itself instead of accumulating.

What is different from Windows:
  * keys are typed through a virtual keyboard (/dev/uinput, python-evdev)
  * "Roblox is in front" comes from Hyprland's IPC socket
  * hotkeys: while a song is playing and the game is in front, the player
    switches Hyprland into its own submap where
        Space       = pause / resume   (runs: pianblox ctl pause, SIGUSR1)
        Backspace   = stop             (runs: pianblox ctl stop,  SIGTERM)
    They fire whatever modifier is down (the player itself holds Shift for the
    black keys, which used to turn Super+End into Super+Shift+End), need no Fn
    key, and every other shortcut is off until the song ends or the game loses
    focus, so a stray Super cannot combine with a played letter into Super+Q.
    Ctrl+C in the terminal also stops.
"""
import argparse
import gc
import json
import os
import re
import shlex
import signal
import socket
import struct
import sys
import time
from collections import namedtuple
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
SONGS = Path(os.environ.get("PIANBLOX_SONGS", ROOT / "songs"))
STATE = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "pianblox"
LOCK = STATE / "playing.lock"

# The 61 piano keys in chromatic order, exactly as the game labels them.
LAYOUT = "1!2@34$5%6^78*9(0qQwWeErtTyYuiIoOpPasSdDfgGhHjJklLzZxcCvVbBnm"
INDEX = {c: i for i, c in enumerate(LAYOUT)}
SHIFTED_SYMBOLS = {"!": "1", "@": "2", "#": "3", "$": "4", "%": "5",
                   "^": "6", "&": "7", "*": "8", "(": "9", ")": "0"}
NOTE_NAMES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]

Key = namedtuple("Key", "code shift idx")

# evdev is imported lazily so --dry-run and the song list work without it.
_ec = None


def ecodes():
    global _ec
    if _ec is None:
        from evdev import ecodes as e
        _ec = e
    return _ec


def base_code(ch):
    """evdev key code for the physical key of a base character (digit or letter)."""
    e = ecodes()
    return getattr(e, "KEY_" + ch.upper())


def resolve_key(c, transpose=0):
    """Character from a schedule -> Key(code, shift, idx) or None if off the keyboard."""
    if transpose and c in INDEX:
        n = INDEX[c] + transpose
        if n < 0 or n >= len(LAYOUT):
            return None
        c = LAYOUT[n]
    idx = INDEX.get(c, -1)
    if c in SHIFTED_SYMBOLS:
        return Key(base_code(SHIFTED_SYMBOLS[c]), True, idx)
    if c.isdigit():
        return Key(base_code(c), False, idx)
    if "a" <= c <= "z":
        return Key(base_code(c), False, idx)
    if "A" <= c <= "Z":
        return Key(base_code(c.lower()), True, idx)
    return None


def note_name(k):
    if k.idx < 0:
        return "?"
    return "%s%d" % (NOTE_NAMES[k.idx % 12], k.idx // 12 + 1)


# --------------------------------------------------------------- schedules --
Note = namedtuple("Note", "at dur key")


def load_schedule(path, speed, transpose):
    notes = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        f = line.split("\t")
        if len(f) < 3:
            continue
        k = resolve_key(f[2].strip()[:1], transpose) if f[2].strip() else None
        if k:
            notes.append(Note(float(f[0]) / speed, float(f[1]) / speed, k))
    return notes


def self_test_notes(transpose):
    """Every key once, slowly, then mixed white+black chords."""
    notes, t = [], 0.0
    for c in LAYOUT:
        k = resolve_key(c, transpose)
        if k:
            notes.append(Note(t, 320.0, k))
            t += 420.0
    t += 800.0
    for pair in ("el", "uk", "pl", "ql", "th", "il"):
        for c in pair:
            k = resolve_key(c, transpose)
            if k:
                notes.append(Note(t, 500.0, k))
        t += 700.0
    return notes


def sheet_notes(sheet, unit_ms, hold_ms, transpose):
    """Virtual Piano letter notes on a flat grid (see README of the Windows version)."""
    sheet = re.sub(r"[ \t]*\|[ \t]*", " ", sheet)
    groups, group, i = [], [], 0
    while i < len(sheet):
        c = sheet[i]
        if c == "[":
            end = sheet.find("]", i)
            if end < 0:
                raise SystemExit("Unclosed '[' at position %d." % i)
            token = [k for k in (resolve_key(ch, transpose) for ch in sheet[i + 1:end]) if k]
            if token:
                group.append(token)
            i = end + 1
        elif c.isspace():
            if group:
                groups.append(group)
                group = []
            spaces, newline = 0, False
            while i < len(sheet) and sheet[i].isspace():
                if sheet[i] in "\r\n":
                    newline = True
                else:
                    spaces += 1
                i += 1
            if not newline:
                groups.extend([] for _ in range(1, spaces))
        else:
            k = resolve_key(c, transpose)
            if k:
                group.append([k])
            i += 1
    if group:
        groups.append(group)
    notes, span = [], 0.0
    for g in groups:
        if not g:
            span += unit_ms
            continue
        slice_ = unit_ms / len(g)
        for token in g:
            for k in token:
                notes.append(Note(span, min(float(hold_ms), slice_ * 0.9), k))
            span += slice_
    return notes


def build_batches(notes):
    """Collapse notes into instants of simultaneous key changes (rounded to 1 ms)."""
    at = {}

    def slot(t):
        key = round(t)
        if key not in at:
            at[key] = {"t": key, "relw": [], "relb": [], "wht": [], "blk": []}
        return at[key]

    for n in notes:
        on = slot(n.at)
        (on["blk"] if n.key.shift else on["wht"]).append(n.key.code)
        off = slot(n.at + n.dur)
        # A key must be lifted with Shift in the same state it was struck with.
        (off["relb"] if n.key.shift else off["relw"]).append(n.key.code)
    return [at[k] for k in sorted(at)]


# ------------------------------------------------------------- hyprland ipc --
SUBMAP = "pianblox"
PAUSE_KEY, STOP_KEY = "space", "BackSpace"
HOTKEYS_HELP = "Space = pause/resume, Backspace = stop"


def hotkey_lua():
    """Lua that (re)defines the submap; sent to Hyprland at the start of every song.

    The binds from the previous song are removed first: defining them twice would make one
    Space press pause and resume at once."""
    exe = shlex.quote(str(HERE / "pianblox.py"))
    opts = "{ ignore_mods = true, dont_inhibit = true }"
    return ("for _, b in ipairs(PIANBLOX_BINDS or {}) do b:remove() end PIANBLOX_BINDS = {} "
            "hl.define_submap(%r, function() "
            "table.insert(PIANBLOX_BINDS, hl.bind(%r, hl.dsp.exec_cmd(%r), %s)) "
            "table.insert(PIANBLOX_BINDS, hl.bind(%r, hl.dsp.exec_cmd(%r), %s)) end)"
            % (SUBMAP, PAUSE_KEY, exe + " ctl pause", opts, STOP_KEY, exe + " ctl stop", opts))


def leave_submap():
    """Back to the normal keymap, but only if we are the ones holding it."""
    try:
        if hypr_request("submap").strip() == SUBMAP:
            hypr_request("dispatch hl.dsp.submap('reset')")
    except OSError:
        pass


def hypr_request(cmd):
    sig = os.environ.get("HYPRLAND_INSTANCE_SIGNATURE")
    rt = os.environ.get("XDG_RUNTIME_DIR")
    if not sig or not rt:
        raise OSError("not running under Hyprland")
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(0.25)
    try:
        s.connect("%s/hypr/%s/.socket.sock" % (rt, sig))
        s.sendall(cmd.encode())
        chunks = []
        while True:
            d = s.recv(65536)
            if not d:
                break
            chunks.append(d)
        return b"".join(chunks).decode("utf-8", "replace")
    finally:
        s.close()


def find_game_window(pattern):
    try:
        for c in json.loads(hypr_request("j/clients")):
            if pattern.search((c.get("class") or "").lower()):
                return c
    except (OSError, ValueError):
        pass
    return None


# ------------------------------------------------------------------- engine --
EV_KEY, EV_SYN = 1, 0
# struct input_event on 64-bit Linux: 8-byte seconds, 8-byte microseconds, u16 type, u16 code, s32 value = 24 bytes.
# (The kernel fills in the timestamp; "=qqHHi" keeps the size at 24 whatever the alignment rules say.)
_EVENT = struct.Struct("=qqHHi")
assert _EVENT.size == 24, "input_event must be 24 bytes"
GO, STOP, REBASE = 0, 1, 2


class Engine:
    def __init__(self, no_input, guard, pattern):
        self.no_input, self.guard, self.pattern = no_input, guard, pattern
        self.stop = False
        self.paused = False
        self.shift_down = False
        self.down_w, self.down_b = set(), set()
        self.buf = bytearray()
        self.rebase = 0.0
        self.paused_ms = self.key_paused_ms = self.focus_paused_ms = 0.0
        self.err = []
        self.ui = None
        self.fd = -1
        self.shift = ecodes().KEY_LEFTSHIFT
        self.hotkeys_ok = False   # submap defined in Hyprland
        self.hotkeys_on = False   # submap currently active

    # -- hotkeys ------------------------------------------------------------
    def setup_hotkeys(self):
        try:
            self.hotkeys_ok = hypr_request("eval " + hotkey_lua()).strip() == "ok"
        except OSError:
            self.hotkeys_ok = False
        return self.hotkeys_ok

    def hotkeys(self, on):
        """Space/Backspace belong to us only while the game is in front."""
        if not self.hotkeys_ok or on == self.hotkeys_on:
            return
        try:
            if on:
                hypr_request("dispatch hl.dsp.submap(%r)" % SUBMAP)
            else:
                leave_submap()
            self.hotkeys_on = on
        except OSError:
            pass

    # -- virtual keyboard ---------------------------------------------------
    def open_keyboard(self):
        if self.no_input:
            return
        from evdev import UInput
        e = ecodes()
        keys = sorted({base_code(c) for c in "1234567890abcdefghijklmnopqrstuvwxyz"} | {e.KEY_LEFTSHIFT})
        try:
            self.ui = UInput({e.EV_KEY: keys}, name="pianblox virtual keyboard")
        except PermissionError:
            raise SystemExit("No access to /dev/uinput. Run linux/install.sh once (needs sudo), "
                             "then try again.")
        self.fd = self.ui.fd
        # Keys sent before the compositor has picked the keyboard up arrive late or not at all,
        # and Hyprland takes about 2 s to do that, so wait until it lists the device.
        t = time.monotonic()
        while time.monotonic() - t < 6.0:
            try:
                kbs = json.loads(hypr_request("j/devices")).get("keyboards", [])
                if any(k.get("name") == "pianblox-virtual-keyboard" for k in kbs):
                    return
            except (OSError, ValueError):
                time.sleep(2.5)   # not Hyprland: give it a fixed head start
                return
            time.sleep(0.05)

    def close_keyboard(self):
        if self.ui:
            self.ui.close()
            self.ui = None

    # -- keys ---------------------------------------------------------------
    def key(self, code, down):
        if self.no_input:
            return
        self.buf += _EVENT.pack(0, 0, EV_KEY, code, 1 if down else 0)
        self.buf += _EVENT.pack(0, 0, EV_SYN, 0, 0)

    def flush(self):
        if self.buf and self.fd >= 0:
            os.write(self.fd, bytes(self.buf))
        self.buf.clear()

    def lift_all(self):
        """Nothing may be left sounding while we are stopped or held."""
        if self.down_b:
            if not self.shift_down:
                self.key(self.shift, True)
                self.shift_down = True
            for k in list(self.down_b):
                self.key(k, False)
            self.down_b.clear()
        if self.shift_down:
            self.key(self.shift, False)
            self.shift_down = False
        for k in list(self.down_w):
            self.key(k, False)
        self.down_w.clear()
        self.flush()

    # -- state --------------------------------------------------------------
    def focused(self):
        if not self.guard:
            return True
        try:
            w = json.loads(hypr_request("j/activewindow"))
        except (OSError, ValueError):
            return True   # cannot tell: do not freeze the song over it
        if not w:
            return False
        # match the window CLASS only: a terminal whose title mentions "roblox" must not count
        return bool(self.pattern.search((w.get("class") or "").lower()))

    def poll(self, t0):
        if self.stop:
            return STOP
        lost = not self.focused()
        self.hotkeys(not lost)
        if not self.paused and not lost:
            return GO
        manual = self.paused
        if lost and not manual:
            print("  [paused - Roblox lost focus]", flush=True)
        elif manual:
            print("  [paused - %s]" % HOTKEYS_HELP, flush=True)
        self.lift_all()
        h0 = (time.perf_counter_ns() - t0) / 1e6
        while True:
            time.sleep(0.03)
            if self.stop:
                return STOP
            front = self.focused()
            self.hotkeys(front)
            if not self.paused and front:
                break
        print("  [resumed]", flush=True)
        self.rebase = (time.perf_counter_ns() - t0) / 1e6 - h0
        self.paused_ms += self.rebase
        if manual:
            self.key_paused_ms += self.rebase
        else:
            self.focus_paused_ms += self.rebase
        return REBASE

    def wait_until(self, t0, ms):
        target = t0 + int(ms * 1e6)
        now_ns = time.perf_counter_ns
        while True:
            r = target - now_ns()
            if r <= 0:
                return GO
            left = r / 1e6
            if left > 4.0:
                st = self.poll(t0)
                if st != GO:
                    return st
                # sleep to within 3 ms of the target, in pieces so signals stay responsive
                time.sleep(min(left - 3.0, 20.0) / 1000.0)
            # else: spin the last stretch

    # -- the performance ----------------------------------------------------
    def play(self, batches, progress_every=150):
        n = len(batches)
        self.err = [0.0] * n
        played, aborted = 0, False
        gc.collect()
        gc.disable()
        t0 = time.perf_counter_ns()
        off = 0.0
        try:
            for i, b in enumerate(batches):
                stopped = False
                while True:
                    at = b["t"] + off
                    st = self.wait_until(t0, at)
                    if st == STOP:
                        stopped = True
                        break
                    if st == REBASE:
                        off += self.rebase
                        continue
                    break
                if stopped or self.stop:
                    aborted = True
                    break
                rw, rb, w, bl = b["relw"], b["relb"], b["wht"], b["blk"]
                # Shift is held, not pulsed; white work first so Shift is left down after a
                # black strike and stays there until a white key needs it up.
                if rw or w:
                    if self.shift_down:
                        self.key(self.shift, False)
                        self.shift_down = False
                    for k in rw:
                        self.key(k, False)
                        self.down_w.discard(k)
                    for k in w:
                        self.key(k, True)
                        self.down_w.add(k)
                if rb or bl:
                    if not self.shift_down:
                        self.key(self.shift, True)
                        self.shift_down = True
                    for k in rb:
                        self.key(k, False)
                        self.down_b.discard(k)
                    for k in bl:
                        self.key(k, True)
                        self.down_b.add(k)
                self.flush()
                self.err[i] = (time.perf_counter_ns() - t0) / 1e6 - at
                played = i + 1
                if progress_every and played % progress_every == 0:
                    s = (time.perf_counter_ns() - t0) / 1e9
                    print("  %d/%d instants  (%d:%02d)" % (played, n, s // 60, s % 60), flush=True)
        finally:
            self.lift_all()
            gc.enable()
        self.total_ms = (time.perf_counter_ns() - t0) / 1e6
        self.played, self.aborted = played, aborted


# ------------------------------------------------------------ single player --
def proc_start_ticks(pid):
    try:
        return int(Path("/proc/%d/stat" % pid).read_text().rsplit(")", 1)[1].split()[19])
    except (OSError, ValueError, IndexError):
        return None


def live_player():
    try:
        info = json.loads(LOCK.read_text())
    except (OSError, ValueError):
        return None
    # the start time guards against a recycled process id
    if proc_start_ticks(info.get("pid", -1)) == info.get("start"):
        return info
    return None


def ctl(action):
    info = live_player()
    if not info:
        # a player killed with SIGKILL cannot give the keyboard back; any hotkey does it instead
        leave_submap()
        print("No pianblox player is running.")
        return 1
    os.kill(info["pid"], {"pause": signal.SIGUSR1, "stop": signal.SIGTERM}[action])
    print("%s sent to '%s' (pid %d)." % (action, info["song"], info["pid"]))
    return 0


# ---------------------------------------------------------------------- cli --
def song_list():
    out = []
    if not SONGS.is_dir():
        return out
    for f in sorted(SONGS.glob("*.sched")):
        title, artist, count, end = f.stem, "", 0, 0.0
        for line in f.read_text(encoding="utf-8").splitlines():
            if line.startswith("#"):
                m = re.match(r"#\s*title:\s*(.+)$", line)
                if m:
                    title = m.group(1).strip()
                m = re.match(r"#\s*artist:\s*(.+)$", line)
                if m:
                    artist = m.group(1).strip()
                continue
            p = line.split("\t")
            if len(p) >= 3:
                count += 1
                end = max(end, float(p[0]) + float(p[1]))
        out.append({"file": f, "key": f.stem, "title": title, "artist": artist,
                    "notes": count, "length": end / 1000.0})
    return out


def show_menu(songs):
    print("\n  Roblox Piano\n  ------------")
    for i, s in enumerate(songs, 1):
        t = s["title"] if len(s["title"]) <= 27 else s["title"][:26] + "."
        a = s["artist"] if len(s["artist"]) <= 23 else s["artist"][:22] + "."
        mm, ss = divmod(int(s["length"]), 60)
        print("  %2d. %-28s%-24s%02d:%02d  %4d notes" % (i, t, a, mm, ss, s["notes"]))
    print("   q. quit\n")


def pick_song(songs, query):
    if query:
        for s in songs:
            if query.lower() in s["key"].lower() or query.lower() in s["title"].lower():
                return s
        raise SystemExit("No song matching '%s'. Run 'pianblox list' to see the list." % query)
    while True:
        show_menu(songs)
        ans = input("  Play which one: ").strip()
        if ans in ("q", "Q", ""):
            print("  Nothing played.")
            return None
        if ans.isdigit() and 1 <= int(ans) <= len(songs):
            return songs[int(ans) - 1]
        for s in songs:
            if ans.lower() in s["key"].lower() or ans.lower() in s["title"].lower():
                return s
        print("  '%s' is not on the list." % ans)


def cmd_play(a):
    song_name = "sheet"
    if a.selftest:
        notes, label, song_name = self_test_notes(a.transpose), "self test: every key, then mixed chords", "self-test"
    elif a.sheet or a.sheetfile:
        text = Path(a.sheetfile).read_text() if a.sheetfile else a.sheet
        notes, label = sheet_notes(text, a.unit_ms, a.hold_ms, a.transpose), "sheet, flat grid at %d ms/note" % a.unit_ms
    else:
        path = a.schedule
        if not path:
            songs = song_list()
            if not songs:
                raise SystemExit("No .sched files in %s. Add one with 'pianblox add'." % SONGS)
            s = pick_song(songs, a.song)
            if not s:
                return 0
            path = s["file"]
            mm, ss = divmod(int(s["length"]), 60)
            print("\n  Playing: %s%s  (%02d:%02d)" % (s["title"], " - " + s["artist"] if s["artist"] else "", mm, ss))
            print("  In the game: %s  (or Ctrl+C here)\n" % HOTKEYS_HELP)
        notes = load_schedule(path, a.speed, a.transpose)
        song_name = Path(path).stem
        label = "schedule '%s' at speed %s" % (Path(path).name, a.speed)
    if not notes:
        raise SystemExit("Nothing to play.")

    batches = build_batches(notes)
    total = batches[-1]["t"] / 1000.0
    print("%s: %d notes, %d instants -> %d:%02d" % (label, len(notes), len(batches), total // 60, total % 60))

    if a.dry_run:
        by_t = {}
        for n in notes:
            by_t.setdefault(round(n.at), []).append(note_name(n.key))
        shown = 0
        for b in batches:
            if not b["wht"] and not b["blk"]:
                continue
            print("%8d ms   %s" % (b["t"], "+".join(by_t.get(b["t"], []))))
            shown += 1
            if shown >= 20:
                print("  ... (%d more instants)" % (len(batches) - 20))
                break
        return 0

    STATE.mkdir(parents=True, exist_ok=True)
    busy = live_player()
    if busy:
        if not a.force:
            print("\n  Already playing '%s' (pid %d)." % (busy["song"], busy["pid"]))
            print("  Run 'pianblox ctl stop' to stop it, or rerun with --force to take over.\n")
            return 1
        print("Stopping '%s' (pid %d)..." % (busy["song"], busy["pid"]))
        os.kill(busy["pid"], signal.SIGTERM)
        for _ in range(30):
            if not live_player():
                break
            time.sleep(0.1)
        if live_player():
            os.kill(busy["pid"], signal.SIGKILL)
            time.sleep(0.3)
    LOCK.write_text(json.dumps({"pid": os.getpid(), "start": proc_start_ticks(os.getpid()),
                                "song": song_name}))

    eng = Engine(a.no_input, not a.no_focus_guard, re.compile(a.window_match))
    signal.signal(signal.SIGINT, lambda *_: setattr(eng, "stop", True))
    signal.signal(signal.SIGTERM, lambda *_: setattr(eng, "stop", True))
    signal.signal(signal.SIGUSR1, lambda *_: setattr(eng, "paused", not eng.paused))
    try:
        eng.open_keyboard()
        win = find_game_window(eng.pattern) if eng.guard else None
        if win:
            try:
                hypr_request("dispatch focuswindow address:%s" % win["address"])
            except OSError:
                pass
        elif eng.guard:
            print("Note: no Roblox/Sober window found yet; the song will wait until one is focused.")
        if not eng.setup_hotkeys():
            print("Note: Hyprland hotkeys unavailable; use 'pianblox ctl pause|stop' or Ctrl+C.")
        for t in range(a.countdown, 0, -1):
            print("Click the Roblox piano... starting in %d   (Ctrl+C / Backspace = stop)" % t, flush=True)
            for _ in range(10):
                if eng.stop:
                    break
                eng.hotkeys(eng.focused())
                time.sleep(0.1)
            if eng.stop:
                break
        try:
            os.nice(-5)
        except (OSError, PermissionError):
            pass
        if eng.stop:
            eng.played, eng.aborted, eng.err, eng.total_ms = 0, True, [], 0.0
        else:
            eng.play(batches)
    finally:
        eng.lift_all()
        eng.close_keyboard()
        eng.hotkeys(False)
        try:
            LOCK.unlink()
        except OSError:
            pass

    if a.trace:
        with open(a.trace, "w") as f:
            for i in range(eng.played):
                f.write("%s\t%s\n" % (batches[i]["t"], batches[i]["t"] + eng.err[i]))
    if eng.key_paused_ms > 0:
        print("Note: held %.1fs on pause." % (eng.key_paused_ms / 1000))
    if eng.focus_paused_ms > 0:
        print("Note: held %.1fs while Roblox was not focused." % (eng.focus_paused_ms / 1000))
    if eng.aborted:
        print("Aborted after %d/%d instants." % (eng.played, len(batches)))
    else:
        errs = [abs(x) for x in eng.err[:eng.played]]
        s = eng.total_ms / 1000
        print("Done. %d/%d instants in %d:%02d  |  timing error: mean %.2f ms, worst %.2f ms"
              % (eng.played, len(batches), s // 60, s % 60, sum(errs) / max(len(errs), 1), max(errs or [0])))
    return 0


def main(argv=None):
    p = argparse.ArgumentParser(prog="pianblox", description="Roblox piano autoplayer (Linux).")
    sub = p.add_subparsers(dest="cmd")

    pl = sub.add_parser("play", help="play a song (default)")
    pl.add_argument("--song", help="play the first song matching this text (skips the menu)")
    pl.add_argument("--schedule", help="play this .sched file directly")
    pl.add_argument("--sheet", help="Virtual Piano letter notes given inline")
    pl.add_argument("--sheetfile", help="Virtual Piano letter notes from a file")
    pl.add_argument("--speed", type=float, default=1.0, help="0.9 slower, 1.1 faster")
    pl.add_argument("--unit-ms", type=int, default=235, help="sheet mode: length of one beat unit")
    pl.add_argument("--hold-ms", type=int, default=90, help="sheet mode: how long each key is held")
    pl.add_argument("--transpose", type=int, default=0, help="semitones to shift everything")
    pl.add_argument("--countdown", type=int, default=5)
    pl.add_argument("--dry-run", action="store_true", help="list the notes without pressing anything")
    pl.add_argument("--no-input", action="store_true", help="run the timing loop but send no keys")
    pl.add_argument("--no-focus-guard", action="store_true")
    pl.add_argument("--window-match", default="sober|roblox",
                    help="regex matched against the focused window's class (default: sober|roblox)")
    pl.add_argument("--self-test", dest="selftest", action="store_true", help="strike all 61 keys one at a time")
    pl.add_argument("--force", action="store_true", help="stop a running player and take over")
    pl.add_argument("--trace", help="write planned-vs-actual press times to this file")

    sub.add_parser("list", help="list the songs")
    c = sub.add_parser("ctl", help="control a running player")
    c.add_argument("action", choices=["pause", "stop"])
    ad = sub.add_parser("add", help="convert a MIDI file into a song (see 'pianblox add -h')")
    ad.add_argument("rest", nargs=argparse.REMAINDER)

    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0].startswith("-"):
        argv = ["play"] + argv
    a = p.parse_args(argv)
    if a.cmd == "list":
        show_menu(song_list())
        return 0
    if a.cmd == "ctl":
        return ctl(a.action)
    if a.cmd == "add":
        sys.path.insert(0, str(HERE))
        import midi_to_sched
        return midi_to_sched.main(a.rest)
    return cmd_play(a)


if __name__ == "__main__":
    sys.exit(main())
