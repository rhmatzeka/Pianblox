using System;
using System.Collections.Generic;
using System.IO;
using System.Text;

// Turns a Standard MIDI File into a playable schedule for the 61-key virtual
// piano: absolute start time, how long to hold, and which key to strike.
//
// Real-world MIDIs are usually a whole band, not a piano part, so tracks can be
// picked out and MIDI channel 10 (drums) is always dropped - its note numbers
// are percussion instruments, not pitches, and would land as noise.
public static class MidiSchedule
{
    // The 61 piano keys in chromatic order, exactly as the game labels them.
    public const string Layout = "1!2@34$5%6^78*9(0qQwWeErtTyYuiIoOpPasSdDfgGhHjJklLzZxcCvVbBnm";
    const int DrumChannel = 9;                  // channel 10, counting from one

    public const double Gap    = 60;   // a key must be up this long before restriking
    public const double MinDur = 130;  // a strike shorter than this risks being silent
    public const double Floor  = 45;
    public const double Duplicate = 90;  // repeats faster than this cannot re-trigger, so fold them

    public class Note { public double At, Dur; public int Idx; }

    // Which physical key a layout position uses. 'q' and 'Q' are one key told
    // apart only by Shift, so they compete for the same piece of hardware.
    public static char PhysicalKey(int idx)
    {
        char c = Layout[idx];
        const string sym = "!@#$%^&*()";
        int n = sym.IndexOf(c);
        if (n >= 0) return "1234567890"[n];
        return char.ToUpperInvariant(c);
    }

    // A "part" is one channel inside one track. Multi-track files give each
    // instrument its own track, but format 0 files put the whole arrangement on
    // a single track and tell instruments apart by channel alone - so the
    // channel, not the track, is what can actually be picked out.
    public class TrackInfo
    {
        public int Index, Track, Channel, Notes, Low, High, MaxPoly;
        public bool IsDrums;
        public string Name = "", Programs = "";
    }

    static int PartKey(int track, int channel) { return track * 16 + channel; }

    public class Result
    {
        public List<Note> Notes = new List<Note>();
        public int Dropped, Lengthened, Shortened, DrumNotesSkipped, Merged, Conflicts;
        public double Bpm, LengthMs;
        public int TempoChanges;
        public int LowIdx = int.MaxValue, HighIdx = int.MinValue;
        public int MaxPoly;
    }

    class RawNote { public int Track, Channel, Pitch, Start, End; }

    static int ReadVarInt(byte[] b, ref int i)
    {
        int v = 0;
        while (true) { byte c = b[i++]; v = (v << 7) | (c & 0x7f); if ((c & 0x80) == 0) return v; }
    }
    static int BE16(byte[] b, int i) { return (b[i] << 8) | b[i + 1]; }
    static int BE32(byte[] b, int i) { return (b[i] << 24) | (b[i + 1] << 16) | (b[i + 2] << 8) | b[i + 3]; }

    static void Parse(string path, out List<RawNote> notes, out List<int[]> tempos,
                      out int division, out Dictionary<int, TrackInfo> info)
    {
        byte[] b = File.ReadAllBytes(path);
        if (b.Length < 14 || Encoding.ASCII.GetString(b, 0, 4) != "MThd")
            throw new Exception("Not a MIDI file (missing MThd header).");
        int ntrk = BE16(b, 10);
        division = BE16(b, 12);
        if ((division & 0x8000) != 0) throw new Exception("SMPTE timecode MIDI is not supported.");

        notes = new List<RawNote>();
        tempos = new List<int[]>();
        info = new Dictionary<int, TrackInfo>();
        int p = 14;

        for (int t = 0; t < ntrk && p + 8 <= b.Length; t++)
        {
            if (Encoding.ASCII.GetString(b, p, 4) != "MTrk") break;
            int len = BE32(b, p + 4), end = p + 8 + len, j = p + 8, tick = 0, running = -1;
            var open = new Dictionary<int, List<int>>();
            var programs = new Dictionary<int, SortedSet<int>>();   // per channel
            string trackName = "";

            while (j < end)
            {
                tick += ReadVarInt(b, ref j);
                int st = b[j];
                if ((st & 0x80) != 0) { running = st; j++; } else st = running;
                int hi = st & 0xf0, ch = st & 0x0f;

                if (hi == 0x90 || hi == 0x80)
                {
                    int n = b[j], vel = b[j + 1]; j += 2;
                    if (hi == 0x90 && vel > 0)
                    {
                        if (!open.ContainsKey(n)) open[n] = new List<int>();
                        open[n].Add(tick);
                        int pk = PartKey(t, ch);
                        if (!info.ContainsKey(pk))
                            info[pk] = new TrackInfo { Track = t, Channel = ch, Low = int.MaxValue, High = int.MinValue };
                        var ti = info[pk];
                        if (ch == DrumChannel) ti.IsDrums = true;
                        ti.Notes++;
                        if (n < ti.Low) ti.Low = n;
                        if (n > ti.High) ti.High = n;
                    }
                    else if (open.ContainsKey(n) && open[n].Count > 0)
                    {
                        notes.Add(new RawNote { Track = t, Channel = ch, Pitch = n, Start = open[n][0], End = tick });
                        open[n].RemoveAt(0);
                    }
                }
                else if (hi == 0xA0 || hi == 0xB0 || hi == 0xE0) j += 2;
                else if (hi == 0xC0)
                {
                    if (!programs.ContainsKey(ch)) programs[ch] = new SortedSet<int>();
                    programs[ch].Add(b[j]); j += 1;
                }
                else if (hi == 0xD0) j += 1;
                else if (st == 0xFF)
                {
                    int meta = b[j++]; int mlen = ReadVarInt(b, ref j);
                    if (meta == 0x51 && mlen == 3)
                        tempos.Add(new int[] { tick, (b[j] << 16) | (b[j + 1] << 8) | b[j + 2] });
                    else if (meta == 0x03)
                        trackName = Encoding.ASCII.GetString(b, j, mlen).Trim();
                    j += mlen;
                }
                else if (st == 0xF0 || st == 0xF7) { int slen = ReadVarInt(b, ref j); j += slen; }
                else throw new Exception("Bad MIDI status byte 0x" + st.ToString("X2") + " at " + j);
            }
            foreach (var kv in info)
            {
                if (kv.Value.Track != t) continue;
                kv.Value.Name = trackName;
                var names = new List<string>();
                if (programs.ContainsKey(kv.Value.Channel))
                    foreach (int pr in programs[kv.Value.Channel]) names.Add(GmName(pr));
                kv.Value.Programs = string.Join(", ", names.ToArray());
            }
            p = end;
        }

        if (tempos.Count == 0) tempos.Add(new int[] { 0, 500000 });
        tempos.Sort((x, y) => x[0].CompareTo(y[0]));
    }

    // Both Describe and Convert number the parts the same way, so what -List
    // prints is exactly what -Parts selects.
    static List<TrackInfo> OrderParts(Dictionary<int, TrackInfo> info, List<RawNote> raw)
    {
        var byPart = new Dictionary<int, List<int[]>>();
        foreach (var n in raw)
        {
            int pk = PartKey(n.Track, n.Channel);
            if (!byPart.ContainsKey(pk)) byPart[pk] = new List<int[]>();
            byPart[pk].Add(new int[] { n.Start, 1 });
            byPart[pk].Add(new int[] { n.End, -1 });
        }
        foreach (var kv in byPart)
        {
            kv.Value.Sort((x, y) => x[0] != y[0] ? x[0].CompareTo(y[0]) : x[1].CompareTo(y[1]));
            int cur = 0, mx = 0;
            foreach (var e in kv.Value) { cur += e[1]; if (cur > mx) mx = cur; }
            if (info.ContainsKey(kv.Key)) info[kv.Key].MaxPoly = mx;
        }
        var list = new List<TrackInfo>(info.Values);
        list.Sort(delegate(TrackInfo a, TrackInfo b)
        {
            if (a.Track != b.Track) return a.Track.CompareTo(b.Track);
            return a.Channel.CompareTo(b.Channel);
        });
        for (int i = 0; i < list.Count; i++) list[i].Index = i + 1;
        return list;
    }

    public static List<TrackInfo> Describe(string path)
    {
        List<RawNote> raw; List<int[]> tempos; int div; Dictionary<int, TrackInfo> info;
        Parse(path, out raw, out tempos, out div, out info);
        return OrderParts(info, raw);
    }

    public static Result Convert(string path, int offset, int[] parts)
    {
        List<RawNote> raw; List<int[]> tempos; int div; Dictionary<int, TrackInfo> info;
        Parse(path, out raw, out tempos, out div, out info);

        HashSet<int> keep = null;
        if (parts != null && parts.Length > 0)
        {
            var wanted = new HashSet<int>(parts);
            keep = new HashSet<int>();
            foreach (var pi in OrderParts(info, raw))
                if (wanted.Contains(pi.Index)) keep.Add(PartKey(pi.Track, pi.Channel));
        }
        var res = new Result();
        res.TempoChanges = tempos.Count;
        // A file often opens with a placeholder tempo, so report the one that
        // actually governs most of the piece.
        int lastTick = 0;
        foreach (var rn in raw) if (rn.End > lastTick) lastTick = rn.End;
        var span = new Dictionary<int, int>();
        for (int i2 = 0; i2 < tempos.Count; i2++)
        {
            int from = tempos[i2][0];
            int to = (i2 + 1 < tempos.Count) ? tempos[i2 + 1][0] : lastTick;
            int d2 = Math.Max(0, to - from);
            if (!span.ContainsKey(tempos[i2][1])) span[tempos[i2][1]] = 0;
            span[tempos[i2][1]] += d2;
        }
        int bestUs = tempos[0][1], bestSpan = -1;
        foreach (var kv2 in span) if (kv2.Value > bestSpan) { bestSpan = kv2.Value; bestUs = kv2.Key; }
        res.Bpm = Math.Round(60000000.0 / bestUs, 1);

        Func<int, double> toMs = tick =>
        {
            double outMs = 0; int last = 0, us = tempos[0][1];
            foreach (var tp in tempos)
            {
                if (tp[0] >= tick) break;
                outMs += (tp[0] - last) * (double)us / div / 1000.0;
                last = tp[0]; us = tp[1];
            }
            return outMs + (tick - last) * (double)us / div / 1000.0;
        };

        foreach (var r in raw)
        {
            if (r.Channel == DrumChannel) { res.DrumNotesSkipped++; continue; }
            if (keep != null && !keep.Contains(PartKey(r.Track, r.Channel))) continue;
            int idx = r.Pitch - offset;
            if (idx < 0 || idx >= Layout.Length) { res.Dropped++; continue; }
            double s = toMs(r.Start);
            res.Notes.Add(new Note { At = s, Dur = toMs(r.End) - s, Idx = idx });
            if (idx < res.LowIdx) res.LowIdx = idx;
            if (idx > res.HighIdx) res.HighIdx = idx;
        }
        if (res.Notes.Count == 0) return res;
        res.Notes.Sort((x, y) => x.At != y.At ? x.At.CompareTo(y.At) : x.Idx.CompareTo(y.Idx));

        // A key cannot be re-struck while it is still held, and a strike that is
        // too short may never sound - so trim or stretch each note to fit.
        // Group by the physical key, not the pitch: a white and a black note on
        // the same key cannot both be struck, however different they sound.
        var byKey = new Dictionary<char, List<Note>>();
        foreach (var n in res.Notes)
        {
            char pk = PhysicalKey(n.Idx);
            if (!byKey.ContainsKey(pk)) byKey[pk] = new List<Note>();
            byKey[pk].Add(n);
        }
        // Arrangements often double the same pitch across tracks, landing two
        // strikes on one key at the same instant. Fold those together first, or
        // the second one looks like a re-strike that cannot possibly fit.
        var merged = new List<Note>();
        foreach (var kv in byKey)
        {
            var l = kv.Value;
            l.Sort((x, y) => x.At.CompareTo(y.At));
            var line = new List<Note>();
            foreach (var n in l)
            {
                if (line.Count > 0 && n.At - line[line.Count - 1].At < Duplicate)
                {
                    var prev = line[line.Count - 1];
                    if (prev.Idx == n.Idx)
                    {
                        // The same pitch doubled across tracks: one note.
                        double end = Math.Max(prev.At + prev.Dur, n.At + n.Dur);
                        prev.Dur = end - prev.At;
                        res.Merged++;
                    }
                    else
                    {
                        // Two different pitches wanting one key at once. Only one
                        // can sound, so keep the higher - that is the melody.
                        res.Conflicts++;
                        if (n.Idx > prev.Idx) { prev.At = n.At; prev.Dur = n.Dur; prev.Idx = n.Idx; }
                    }
                    continue;
                }
                line.Add(n);
            }

            for (int i = 0; i < line.Count; i++)
            {
                double want = Math.Max(line[i].Dur, MinDur);
                if (i + 1 < line.Count)
                {
                    // The key must come back up before it can be struck again, so
                    // the gap wins over the preferred length. On fast repeats it
                    // shrinks with the interval instead of being dropped.
                    double interval = line[i + 1].At - line[i].At;
                    double gap = Math.Min(Gap, interval * 0.45);
                    double room = interval - gap;
                    want = Math.Min(want, room);
                    want = Math.Max(want, Math.Min(Floor, room));
                }
                else want = Math.Max(want, Floor);

                if (want > line[i].Dur) res.Lengthened++;
                else if (want < line[i].Dur) res.Shortened++;
                line[i].Dur = want;
            }
            merged.AddRange(line);
        }
        merged.Sort((x, y) => x.At != y.At ? x.At.CompareTo(y.At) : x.Idx.CompareTo(y.Idx));
        res.Notes = merged;

        var ev = new List<double[]>();
        foreach (var n in res.Notes)
        {
            ev.Add(new double[] { n.At, 1 });
            ev.Add(new double[] { n.At + n.Dur, -1 });
            if (n.At + n.Dur > res.LengthMs) res.LengthMs = n.At + n.Dur;
        }
        ev.Sort((x, y) => x[0] != y[0] ? x[0].CompareTo(y[0]) : x[1].CompareTo(y[1]));
        int c = 0;
        foreach (var e in ev) { c += (int)e[1]; if (c > res.MaxPoly) res.MaxPoly = c; }
        return res;
    }

    public static string NoteName(int idx)
    {
        string[] nn = { "C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B" };
        return nn[idx % 12] + (idx / 12 + 1);
    }
    public static string MidiName(int pitch)
    {
        string[] nn = { "C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B" };
        return nn[pitch % 12] + (pitch / 12 - 1);
    }

    static string GmName(int p)
    {
        string[] g = {
        "Acoustic Grand","Bright Piano","Electric Grand","Honky-tonk","Electric Piano 1","Electric Piano 2","Harpsichord","Clavinet",
        "Celesta","Glockenspiel","Music Box","Vibraphone","Marimba","Xylophone","Tubular Bells","Dulcimer",
        "Drawbar Organ","Percussive Organ","Rock Organ","Church Organ","Reed Organ","Accordion","Harmonica","Tango Accordion",
        "Nylon Guitar","Steel Guitar","Jazz Guitar","Clean Guitar","Muted Guitar","Overdriven Guitar","Distortion Guitar","Guitar Harmonics",
        "Acoustic Bass","Finger Bass","Pick Bass","Fretless Bass","Slap Bass 1","Slap Bass 2","Synth Bass 1","Synth Bass 2",
        "Violin","Viola","Cello","Contrabass","Tremolo Strings","Pizzicato Strings","Orchestral Harp","Timpani",
        "String Ensemble 1","String Ensemble 2","Synth Strings 1","Synth Strings 2","Choir Aahs","Voice Oohs","Synth Voice","Orchestra Hit",
        "Trumpet","Trombone","Tuba","Muted Trumpet","French Horn","Brass Section","Synth Brass 1","Synth Brass 2",
        "Soprano Sax","Alto Sax","Tenor Sax","Baritone Sax","Oboe","English Horn","Bassoon","Clarinet",
        "Piccolo","Flute","Recorder","Pan Flute","Blown Bottle","Shakuhachi","Whistle","Ocarina",
        "Square Lead","Sawtooth Lead","Calliope","Chiff","Charang","Voice Lead","Fifths","Bass+Lead",
        "New Age Pad","Warm Pad","Polysynth","Choir Pad","Bowed Pad","Metallic Pad","Halo Pad","Sweep Pad",
        "Rain","Soundtrack","Crystal","Atmosphere","Brightness","Goblins","Echoes","Sci-Fi",
        "Sitar","Banjo","Shamisen","Koto","Kalimba","Bagpipe","Fiddle","Shanai",
        "Tinkle Bell","Agogo","Steel Drums","Woodblock","Taiko","Melodic Tom","Synth Drum","Reverse Cymbal",
        "Guitar Fret Noise","Breath Noise","Seashore","Bird Tweet","Telephone","Helicopter","Applause","Gunshot" };
        return (p >= 0 && p < g.Length) ? g[p] : ("Program " + p);
    }
}
