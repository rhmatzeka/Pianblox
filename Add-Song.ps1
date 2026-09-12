<#
  Turns a .mid file into a song this player can perform.

    .\Add-Song.ps1 -Midi ".\midi\song.mid" -List
        show what is inside the file, so you can pick the parts worth playing

    .\Add-Song.ps1 -Midi ".\midi\song.mid" -Title "Song" -Artist "Someone" -Tracks 2,4,6
        convert only those tracks into songs\song.sched

  Most MIDIs are a whole band. Playing every track at once on one piano sounds
  like mud, so pick a melody track plus a bass or accompaniment track. Drums
  (MIDI channel 10) are always dropped - those note numbers are percussion
  instruments, not pitches.

  -Offset slides the piece across the keyboard in semitones. The default puts
  MIDI note 36 on the leftmost key; the report says if notes fell off the ends.
#>
param(
  [Parameter(Mandatory=$true)][string]$Midi,
  [switch]$List,
  [string]$Title,
  [string]$Artist = '',
  [string]$Name,
  [object]$Tracks,
  [int]$Offset = 36,
  [switch]$Force
)

$ErrorActionPreference = 'Stop'

# Name the type after a hash of its source, so an edited converter is picked up
# instead of a stale copy lingering in the session.
$src  = Get-Content -Raw -LiteralPath (Join-Path $PSScriptRoot 'MidiToSchedule.cs')
$md5  = [Security.Cryptography.MD5]::Create()
$tag  = ([BitConverter]::ToString($md5.ComputeHash([Text.Encoding]::UTF8.GetBytes($src)))).Replace('-','').Substring(0,8)
$type = "MidiSchedule_$tag"
if (-not ($type -as [type])) {
  Add-Type -TypeDefinition ($src.Replace('class MidiSchedule', "class $type")) -ErrorAction Stop
}
$Conv = $type -as [type]

$midiPath = (Resolve-Path -LiteralPath $Midi).Path

# Taken as text on purpose: launched with -File, PowerShell reads "-Tracks 2,4,6"
# as the single number 246, treating the commas as thousands separators.
$trackList = $null
if ($Tracks) {
  $trackList = [int[]]@(
    (($Tracks -join ',') -split '[,;\s]+') | Where-Object { $_ -ne '' } | ForEach-Object {
      $n = 0
      if (-not [int]::TryParse($_, [ref]$n)) { throw "-Tracks: '$_' is not a track number." }
      $n
    }
  )
  if ($trackList.Count -eq 0) { throw "-Tracks was given but no track numbers were found." }
}

if ($List) {
  Write-Host ''
  Write-Host ("  $([IO.Path]::GetFileName($midiPath))") -ForegroundColor Cyan
  Write-Host ('  {0,-5} {1,-4} {2,-7} {3,-14} {4,-5} {5}' -f 'track','ch','notes','range','poly','instrument') -ForegroundColor DarkGray
  foreach ($t in $Conv::Describe($midiPath)) {
    $range = '{0}-{1}' -f $Conv::MidiName($t.Low), $Conv::MidiName($t.High)
    $line  = '  {0,-5} {1,-4} {2,-7} {3,-14} {4,-5} {5}' -f `
             $t.Index, ($t.Channel + 1), $t.Notes, $range, $t.MaxPoly, $t.Programs
    if ($t.IsDrums) { Write-Host ($line + '   [drums - always skipped]') -ForegroundColor DarkYellow }
    else { Write-Host $line }
  }
  Write-Host ''
  Write-Host '  Pick a melody track plus a bass or accompaniment track, then rerun with -Tracks' -ForegroundColor DarkGray
  Write-Host '  A melody is usually the one with poly 1-2 in the upper range.' -ForegroundColor DarkGray
  return
}

if (-not $Title) { $Title = [IO.Path]::GetFileNameWithoutExtension($midiPath) }
if (-not $Name)  { $Name  = ($Title -replace '[^\w\- ]','' -replace '\s+','-').ToLower() }

$songs = Join-Path $PSScriptRoot 'songs'
if (-not (Test-Path $songs)) { New-Item -ItemType Directory -Path $songs | Out-Null }
$out = Join-Path $songs "$Name.sched"
if ((Test-Path $out) -and -not $Force) { throw "$out already exists. Pass -Force to overwrite." }

$r = $Conv::Convert($midiPath, $Offset, $trackList)
if ($r.Notes.Count -eq 0) { throw "No playable notes. Check -Tracks (see -List) or -Offset." }

$layout = $Conv::Layout
$sb = New-Object System.Text.StringBuilder
[void]$sb.AppendLine("# title: $Title")
[void]$sb.AppendLine("# artist: $Artist")
[void]$sb.AppendLine("# source: $([IO.Path]::GetFileName($midiPath))")
if ($trackList) { [void]$sb.AppendLine("# tracks: $($trackList -join ',')") }
[void]$sb.AppendLine("# start_ms`tdur_ms`tkey")
foreach ($n in $r.Notes) {
  [void]$sb.AppendLine(('{0:0.0}' -f $n.At) + "`t" + ('{0:0.0}' -f $n.Dur) + "`t" + $layout[$n.Idx])
}
[IO.File]::WriteAllText($out, $sb.ToString())

Write-Host "Wrote $out" -ForegroundColor Green
Write-Host ("  {0} notes, {1:mm\:ss}, {2} BPM ({3} tempo changes)" -f `
  $r.Notes.Count, [timespan]::FromMilliseconds($r.LengthMs), $r.Bpm, $r.TempoChanges)
Write-Host ("  range {0} .. {1},  up to {2} keys at once" -f `
  $Conv::NoteName($r.LowIdx), $Conv::NoteName($r.HighIdx), $r.MaxPoly)
Write-Host ("  {0} notes lengthened, {1} shortened to stay playable" -f $r.Lengthened, $r.Shortened)
if ($r.Merged -gt 0)    { Write-Host ("  {0} doubled notes merged" -f $r.Merged) -ForegroundColor DarkGray }
if ($r.Conflicts -gt 0) { Write-Host ("  {0} notes dropped where two pitches wanted one physical key" -f $r.Conflicts) -ForegroundColor DarkGray }
if ($r.DrumNotesSkipped -gt 0) { Write-Host ("  {0} drum notes skipped" -f $r.DrumNotesSkipped) -ForegroundColor DarkGray }
if ($r.Dropped -gt 0) {
  Write-Host ("  WARNING: {0} notes fell outside the 61 keys - try a different -Offset" -f $r.Dropped) -ForegroundColor Yellow
}
