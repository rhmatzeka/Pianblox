"""
MIDI -> .sched converter for the 61-key Roblox piano (Linux port of MidiToSchedule.cs + Add-Song.ps1).

Same rules as the Windows version:
  * a "part" is one channel inside one track (format 0 files tell instruments apart by channel only)
  * MIDI channel 10 (drums) is always dropped
  * a key must be up 60 ms before it is struck again, a strike shorter than 130 ms may be silent
  * the same pitch doubled across tracks is folded into one note
  * two different pitches wanting one physical key (q / Q share hardware): the higher one wins

Usage (through the player):
    pianblox add song.mid --list
    pianblox add song.mid --title "Song" --artist "Someone" --parts 1,3
"""
import argparse
import os
import re
import sys
from pathlib import Path

LAYOUT = "1!2@34$5%6^78*9(0qQwWeErtTyYuiIoOpPasSdDfgGhHjJklLzZxcCvVbBnm"
DRUM_CHANNEL = 9            # channel 10, counting from one
GAP = 60.0                  # a key must be up this long before restriking
MIN_DUR = 130.0             # a strike shorter than this risks being silent
FLOOR = 45.0
DUPLICATE = 90.0            # repeats faster than this cannot re-trigger, so fold them
NOTE_NAMES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]

GM = ["Acoustic Grand", "Bright Piano", "Electric Grand", "Honky-tonk", "Electric Piano 1", "Electric Piano 2", "Harpsichord", "Clavinet",
      "Celesta", "Glockenspiel", "Music Box", "Vibraphone", "Marimba", "Xylophone", "Tubular Bells", "Dulcimer",
      "Drawbar Organ", "Percussive Organ", "Rock Organ", "Church Organ", "Reed Organ", "Accordion", "Harmonica", "Tango Accordion",
      "Nylon Guitar", "Steel Guitar", "Jazz Guitar", "Clean Guitar", "Muted Guitar", "Overdriven Guitar", "Distortion Guitar", "Guitar Harmonics",
      "Acoustic Bass", "Finger Bass", "Pick Bass", "Fretless Bass", "Slap Bass 1", "Slap Bass 2", "Synth Bass 1", "Synth Bass 2",
      "Violin", "Viola", "Cello", "Contrabass", "Tremolo Strings", "Pizzicato Strings", "Orchestral Harp", "Timpani",
      "String Ensemble 1", "String Ensemble 2", "Synth Strings 1", "Synth Strings 2", "Choir Aahs", "Voice Oohs", "Synth Voice", "Orchestra Hit",
      "Trumpet", "Trombone", "Tuba", "Muted Trumpet", "French Horn", "Brass Section", "Synth Brass 1", "Synth Brass 2",
      "Soprano Sax", "Alto Sax", "Tenor Sax", "Baritone Sax", "Oboe", "English Horn", "Bassoon", "Clarinet",
      "Piccolo", "Flute", "Recorder", "Pan Flute", "Blown Bottle", "Shakuhachi", "Whistle", "Ocarina",
      "Square Lead", "Sawtooth Lead", "Calliope", "Chiff", "Charang", "Voice Lead", "Fifths", "Bass+Lead",
      "New Age Pad", "Warm Pad", "Polysynth", "Choir Pad", "Bowed Pad", "Metallic Pad", "Halo Pad", "Sweep Pad",
      "Rain", "Soundtrack", "Crystal", "Atmosphere", "Brightness", "Goblins", "Echoes", "Sci-Fi",
      "Sitar", "Banjo", "Shamisen", "Koto", "Kalimba", "Bagpipe", "Fiddle", "Shanai",
      "Tinkle Bell", "Agogo", "Steel Drums", "Woodblock", "Taiko", "Melodic Tom", "Synth Drum", "Reverse Cymbal",
      "Guitar Fret Noise", "Breath Noise", "Seashore", "Bird Tweet", "Telephone", "Helicopter", "Applause", "Gunshot"]


def gm_name(p):
    return GM[p] if 0 <= p < len(GM) else "Program %d" % p


def note_name(idx):
    return "%s%d" % (NOTE_NAMES[idx % 12], idx // 12 + 1)


def midi_name(pitch):
    return "%s%d" % (NOTE_NAMES[pitch % 12], pitch // 12 - 1)


def physical_key(idx):
    """'q' and 'Q' are one key told apart only by Shift, so they compete for the same hardware."""
    c = LAYOUT[idx]
    sym = "!@#$%^&*()"
    if c in sym:
        return "1234567890"[sym.index(c)]
    return c.upper()


class Part:
    def __init__(self, track, channel):
        self.index = 0
        self.track, self.channel = track, channel
        self.notes = 0
        self.low, self.high = 10 ** 9, -10 ** 9
        self.max_poly = 0
        self.is_drums = False
        self.name, self.programs = "", ""


class Note:
    __slots__ = ("at", "dur", "idx")

    def __init__(self, at, dur, idx):
        self.at, self.dur, self.idx = at, dur, idx


class Result:
    def __init__(self):
        self.notes = []
        self.dropped = self.lengthened = self.shortened = self.drum_skipped = self.merged = self.conflicts = 0
        self.bpm = self.length_ms = 0.0
        self.tempo_changes = 0
        self.low_idx, self.high_idx = 10 ** 9, -10 ** 9
        self.max_poly = 0


def _varint(b, i):
    v = 0
    while True:
        c = b[i]
        i += 1
        v = (v << 7) | (c & 0x7F)
        if not c & 0x80:
            return v, i


def parse(path):
    b = Path(path).read_bytes()
    if len(b) < 14 or b[0:4] != b"MThd":
        raise SystemExit("Not a MIDI file (missing MThd header).")
    ntrk = (b[10] << 8) | b[11]
    division = (b[12] << 8) | b[13]
    if division & 0x8000:
        raise SystemExit("SMPTE timecode MIDI is not supported.")

    raw, tempos, info = [], [], {}
    p = 14
    for t in range(ntrk):
        if p + 8 > len(b) or b[p:p + 4] != b"MTrk":
            break
        length = int.from_bytes(b[p + 4:p + 8], "big")
        end, j, tick, running = p + 8 + length, p + 8, 0, -1
        open_notes, programs, track_name = {}, {}, ""
        while j < end:
            d, j = _varint(b, j)
            tick += d
            st = b[j]
            if st & 0x80:
                running = st
                j += 1
            else:
                st = running
            hi, ch = st & 0xF0, st & 0x0F
            if hi in (0x90, 0x80):
                n, vel = b[j], b[j + 1]
                j += 2
                if hi == 0x90 and vel > 0:
                    open_notes.setdefault(n, []).append(tick)
                    pk = t * 16 + ch
                    if pk not in info:
                        info[pk] = Part(t, ch)
                    ti = info[pk]
                    if ch == DRUM_CHANNEL:
                        ti.is_drums = True
                    ti.notes += 1
                    ti.low, ti.high = min(ti.low, n), max(ti.high, n)
                elif open_notes.get(n):
                    raw.append((t, ch, n, open_notes[n].pop(0), tick))
            elif hi in (0xA0, 0xB0, 0xE0):
                j += 2
            elif hi == 0xC0:
                programs.setdefault(ch, set()).add(b[j])
                j += 1
            elif hi == 0xD0:
                j += 1
            elif st == 0xFF:
                meta = b[j]
                j += 1
                mlen, j = _varint(b, j)
                if meta == 0x51 and mlen == 3:
                    tempos.append((tick, (b[j] << 16) | (b[j + 1] << 8) | b[j + 2]))
                elif meta == 0x03:
                    track_name = b[j:j + mlen].decode("ascii", "replace").strip()
                j += mlen
            elif st in (0xF0, 0xF7):
                slen, j = _varint(b, j)
                j += slen
            else:
                raise SystemExit("Bad MIDI status byte 0x%02X at %d" % (st, j))
        for part in info.values():
            if part.track != t:
                continue
            part.name = track_name
            part.programs = ", ".join(gm_name(x) for x in sorted(programs.get(part.channel, ())))
        p = end
    if not tempos:
        tempos.append((0, 500000))
    tempos.sort(key=lambda x: x[0])
    return raw, tempos, division, info


def order_parts(info, raw):
    """Both --list and the converter number the parts the same way."""
    by_part = {}
    for (t, ch, _n, start, end) in raw:
        by_part.setdefault(t * 16 + ch, []).extend([(start, 1), (end, -1)])
    for pk, ev in by_part.items():
        ev.sort()
        cur = mx = 0
        for _t, d in ev:
            cur += d
            mx = max(mx, cur)
        if pk in info:
            info[pk].max_poly = mx
    parts = sorted(info.values(), key=lambda x: (x.track, x.channel))
    for i, part in enumerate(parts, 1):
        part.index = i
    return parts


def describe(path):
    raw, _tempos, _div, info = parse(path)
    return order_parts(info, raw)


def convert(path, offset, parts=None):
    raw, tempos, div, info = parse(path)
    keep = None
    if parts:
        wanted = set(parts)
        keep = {pi.track * 16 + pi.channel for pi in order_parts(info, raw) if pi.index in wanted}
    res = Result()
    res.tempo_changes = len(tempos)

    # a file often opens with a placeholder tempo, so report the one that governs most of the piece
    last_tick = max((r[4] for r in raw), default=0)
    span = {}
    for i, (frm, us) in enumerate(tempos):
        to = tempos[i + 1][0] if i + 1 < len(tempos) else last_tick
        span[us] = span.get(us, 0) + max(0, to - frm)
    best_us, best_span = tempos[0][1], -1
    for us, sp in span.items():
        if sp > best_span:
            best_us, best_span = us, sp
    res.bpm = round(60000000.0 / best_us, 1)

    def to_ms(tick):
        out, last, us = 0.0, 0, tempos[0][1]
        for tt, tus in tempos:
            if tt >= tick:
                break
            out += (tt - last) * float(us) / div / 1000.0
            last, us = tt, tus
        return out + (tick - last) * float(us) / div / 1000.0

    for (t, ch, pitch, start, end) in raw:
        if ch == DRUM_CHANNEL:
            res.drum_skipped += 1
            continue
        if keep is not None and (t * 16 + ch) not in keep:
            continue
        idx = pitch - offset
        if idx < 0 or idx >= len(LAYOUT):
            res.dropped += 1
            continue
        s = to_ms(start)
        res.notes.append(Note(s, to_ms(end) - s, idx))
        res.low_idx, res.high_idx = min(res.low_idx, idx), max(res.high_idx, idx)
    if not res.notes:
        return res
    res.notes.sort(key=lambda n: (n.at, n.idx))

    # group by the PHYSICAL key, not the pitch: a white and a black note on one key cannot both be struck
    by_key = {}
    for n in res.notes:
        by_key.setdefault(physical_key(n.idx), []).append(n)
    merged = []
    for _k, lst in by_key.items():
        lst.sort(key=lambda n: n.at)
        line = []
        for n in lst:
            if line and n.at - line[-1].at < DUPLICATE:
                prev = line[-1]
                if prev.idx == n.idx:
                    # the same pitch doubled across tracks: one note
                    end = max(prev.at + prev.dur, n.at + n.dur)
                    prev.dur = end - prev.at
                    res.merged += 1
                else:
                    # two different pitches wanting one key at once: keep the higher (the melody)
                    res.conflicts += 1
                    if n.idx > prev.idx:
                        prev.at, prev.dur, prev.idx = n.at, n.dur, n.idx
                continue
            line.append(n)
        for i, cur in enumerate(line):
            want = max(cur.dur, MIN_DUR)
            if i + 1 < len(line):
                # the key must come back up before it can be struck again, so the gap wins over the length
                interval = line[i + 1].at - cur.at
                gap = min(GAP, interval * 0.45)
                room = interval - gap
                want = min(want, room)
                want = max(want, min(FLOOR, room))
            else:
                want = max(want, FLOOR)
            if want > cur.dur:
                res.lengthened += 1
            elif want < cur.dur:
                res.shortened += 1
            cur.dur = want
        merged.extend(line)
    merged.sort(key=lambda n: (n.at, n.idx))
    res.notes = merged

    ev = []
    for n in res.notes:
        ev.append((n.at, 1))
        ev.append((n.at + n.dur, -1))
        res.length_ms = max(res.length_ms, n.at + n.dur)
    ev.sort()
    c = 0
    for _t, d in ev:
        c += d
        res.max_poly = max(res.max_poly, c)
    return res


def main(argv):
    ap = argparse.ArgumentParser(prog="pianblox add", description="Turn a .mid file into a song this player can perform.")
    ap.add_argument("midi")
    ap.add_argument("--list", action="store_true", help="show what is inside the file, to pick the parts worth playing")
    ap.add_argument("--title")
    ap.add_argument("--artist", default="")
    ap.add_argument("--name", help="file name of the song (default: derived from the title)")
    ap.add_argument("--parts", help="comma-separated part numbers from --list, e.g. 1,3")
    ap.add_argument("--offset", type=int, default=36,
                    help="slide the piece across the keyboard in semitones (default 36: MIDI note 36 on the leftmost key)")
    ap.add_argument("--force", action="store_true", help="overwrite an existing song")
    a = ap.parse_args(argv)

    midi = Path(a.midi).expanduser()
    if not midi.is_file():
        raise SystemExit("No such file: %s" % midi)

    if a.list:
        print("\n  %s" % midi.name)
        print("  %-5s %-4s %-4s %-7s %-14s %-5s %s" % ("part", "trk", "ch", "notes", "range", "poly", "instrument"))
        for t in describe(midi):
            rng = "%s-%s" % (midi_name(t.low), midi_name(t.high))
            line = "  %-5s %-4s %-4s %-7s %-14s %-5s %s" % (t.index, t.track, t.channel + 1, t.notes, rng, t.max_poly, t.programs)
            print(line + ("   [drums - always skipped]" if t.is_drums else ""))
        print("\n  Pick a melody part plus a bass or accompaniment part, then rerun with --parts")
        print("  A melody is usually the one with poly 1-2 in the upper range.")
        return 0

    parts = None
    if a.parts:
        try:
            parts = [int(x) for x in re.split(r"[,;\s]+", a.parts) if x]
        except ValueError:
            raise SystemExit("--parts: '%s' is not a list of part numbers." % a.parts)
        if not parts:
            raise SystemExit("--parts was given but no part numbers were found.")

    title = a.title or midi.stem
    name = a.name or re.sub(r"\s+", "-", re.sub(r"[^\w\- ]", "", title)).lower()
    songs = Path(os.environ.get("PIANBLOX_SONGS", Path(__file__).resolve().parent.parent / "songs"))
    songs.mkdir(parents=True, exist_ok=True)
    out = songs / (name + ".sched")
    if out.exists() and not a.force:
        raise SystemExit("%s already exists. Pass --force to overwrite." % out)

    r = convert(midi, a.offset, parts)
    if not r.notes:
        raise SystemExit("No playable notes. Check --parts (see --list) or --offset.")

    lines = ["# title: %s" % title, "# artist: %s" % a.artist, "# source: %s" % midi.name]
    if parts:
        lines.append("# parts: %s" % ",".join(str(x) for x in parts))
    lines.append("# start_ms\tdur_ms\tkey")
    for n in r.notes:
        lines.append("%.1f\t%.1f\t%s" % (n.at, n.dur, LAYOUT[n.idx]))
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")

    mm, ss = divmod(int(r.length_ms / 1000), 60)
    print("Wrote %s" % out)
    print("  %d notes, %02d:%02d, %s BPM (%d tempo changes)" % (len(r.notes), mm, ss, r.bpm, r.tempo_changes))
    print("  range %s .. %s,  up to %d keys at once" % (note_name(r.low_idx), note_name(r.high_idx), r.max_poly))
    print("  %d notes lengthened, %d shortened to stay playable" % (r.lengthened, r.shortened))
    if r.merged:
        print("  %d doubled notes merged" % r.merged)
    if r.conflicts:
        print("  %d notes dropped where two pitches wanted one physical key" % r.conflicts)
    if r.drum_skipped:
        print("  %d drum notes skipped" % r.drum_skipped)
    if r.dropped:
        print("  WARNING: %d notes fell outside the 61 keys - try a different --offset" % r.dropped)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
