# Roblox Piano Player

Plays MIDI files on a Roblox virtual piano by sending real keystrokes, with
timing accurate to about a millisecond.

Windows only. No dependencies — the MIDI parser and the playback engine are
C# compiled at run time by PowerShell.

## Running

Double-click `PLAY.bat`, or:

```powershell
.\Play.ps1
```

```
  Roblox Piano
  ------------
   1. Canon in D                  Pachelbel         05:52   824 notes
   q. quit

  Play which one:
```

Pick a number, then click the Roblox window during the five-second countdown.
Leave the game's own transpose ("Pindahkan") at 0.

While playing: **END** stops, **F8** pauses and resumes. Both are global, so
they work while Roblox has focus. Alt-tabbing away pauses automatically and
lifts every key, so it never types into another window.

```powershell
.\Play.ps1 -Song canon        # skip the menu
.\Play.ps1 -Song canon -Speed 0.9
```

## Adding a song

Put a `.mid` file in `midi\`, look inside it, then convert the parts worth
playing:

```powershell
.\Add-Song.ps1 -Midi ".\midi\song.mid" -List
```

```
  track ch   notes   range          poly  instrument
  2     2    534     C2-D3          2     Acoustic Bass
  4     4    336     G3-D5          2     Alto Sax
  10    10   231     B1-A5          2     Acoustic Grand   [drums - always skipped]
```

```powershell
.\Add-Song.ps1 -Midi ".\midi\song.mid" -Title "Song" -Artist "Someone" -Tracks "2,4"
```

Most MIDI files are a whole band. One piano playing every track at once is
mud, and the game has no volume control, so a melody buried under a dense
accompaniment is simply inaudible — pick a melody track plus a bass or
accompaniment track. Drums are dropped automatically: their note numbers are
percussion instruments, not pitches.

`-Offset` slides the piece across the keyboard in semitones (default 36, which
puts MIDI note 36 on the leftmost key). The report says how many notes fell off
the ends, and what key the result is in.

Songs are not included in this repository. The Canon is here as a sample
because Pachelbel died in 1706; add your own MIDIs for anything else.

## How it works

`Play.ps1` picks a song, `Play-Piano.ps1` builds a schedule of key events, and
`Engine.cs` performs it. `MidiToSchedule.cs` does the MIDI conversion.

A few things that turned out to matter more than expected:

**The playback loop is C#, not PowerShell.** The interpreter allocates on every
note and the resulting GC pauses are far larger than the timing tolerance. In
C# with an allocation-free loop, mean error is about 0.01 ms against 2 ms in
PowerShell.

**Sleep is not accurate enough on its own.** `Thread.Sleep` overshoots by up to
a timer tick even at 1 ms resolution, so the loop sleeps to within 15 ms of the
target and spins the rest. Sleeps are chopped into 20 ms pieces so the hotkeys
stay responsive.

**The schedule is absolute.** Every instant is timed from the start, so a
hiccup corrects itself on the next note instead of accumulating.

**Shift is held, not pulsed.** Black keys are Shift plus a white key. A Shift
press measured in microseconds can be gone by the time the game reads the
modifier, so it is held until a white key needs it up — and it must match the
key's colour when a key is *lifted* too, or the game silences the wrong note
and the old one hangs.

**White and black keys share hardware.** `q` and `Q` are one physical key. Two
pitches wanting that key at the same moment cannot both sound, so the converter
groups by physical key, not by pitch, and keeps the higher note.

**A key must come back up before it can be struck again.** Strikes are given a
60 ms gap and a 130 ms minimum length; on fast repeats the gap shrinks with the
interval rather than being dropped. Repeats too fast to re-trigger, and the
same pitch doubled across tracks, are folded into one note.

Only one player may run at a time — two would fight over the keyboard. A lock
file holds the process id and its start time, so a recycled id is not mistaken
for a live player.

## Options

| | |
|---|---|
| `-Song <name>` | play without the menu; matches part of the title |
| `-Speed <n>` | `0.9` slower, `1.1` faster; rhythm stays proportional |
| `-Countdown <s>` | seconds before playing, default 5 |
| `-DryRun` | list the notes without pressing anything |
| `-SelfTest` | strike all 61 keys one at a time, to find a dead one |
| `-Force` | take over from a player that is already running |
| `-Trace <file>` | record planned against actual press times |
