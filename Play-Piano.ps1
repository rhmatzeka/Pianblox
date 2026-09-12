<#
  Roblox virtual-piano autoplayer.

  Two ways to feed it:

  -Schedule <file>   a timed schedule, one note per line: start_ms, dur_ms, key.
                     Notes keep their own lengths and may overlap, so the
                     rhythm is whatever the source actually plays.

  -SheetFile <file>  Virtual Piano letter notes. These carry no durations, so
                     every note is given an equal slice of a fixed grid:
                       a b c      whitespace-separated groups, one unit each
                       abc        notes in one group subdivide that unit
                       [ab] [a b] chord, spaces inside brackets allowed
                       "  "       two or more spaces = that many beats of rest
                       |          bar line; it and its spaces are one separator

  While playing:  END = stop,  F8 = pause/resume,  Ctrl+C = stop.
#>
param(
  [string]$Schedule,
  [string]$SheetFile,
  [string]$Sheet,
  [double]$Speed  = 1.0,   # schedule mode: >1 faster, <1 slower
  [int]$UnitMs    = 235,   # sheet mode: duration of one beat unit
  [int]$HoldMs    = 90,    # sheet mode: how long each key is held
  [int]$Transpose = 0,     # semitones to shift everything
  [int]$Countdown = 5,
  [switch]$NoFocusGuard,
  [switch]$Force,          # stop a player that is already running, then take over
  [switch]$SelfTest,      # walk every key slowly so dead ones are obvious
  [switch]$DryRun,
  [switch]$NoInput,        # run the timing loop but send no keystrokes
  [string]$Trace           # write planned-vs-actual press times to this file
)

$ErrorActionPreference = 'Stop'

# The 61 piano keys in chromatic order, exactly as the game labels them.
$Layout = '1!2@34$5%6^78*9(0qQwWeErtTyYuiIoOpPasSdDfgGhHjJklLzZxcCvVbBnm'
$Index  = @{}
for ($i = 0; $i -lt $Layout.Length; $i++) { $Index[$Layout[$i]] = $i }

$ShiftedSymbols = @{
  '!' = '1'; '@' = '2'; '#' = '3'; '$' = '4'; '%' = '5'
  '^' = '6'; '&' = '7'; '*' = '8'; '(' = '9'; ')' = '0'
}
$NoteNames = @('C','C#','D','D#','E','F','F#','G','G#','A','A#','B')

function Resolve-Key([char]$c) {
  if ($Transpose -ne 0 -and $Index.ContainsKey($c)) {
    $n = $Index[$c] + $Transpose
    if ($n -lt 0 -or $n -ge $Layout.Length) { return $null }   # off the keyboard
    $c = $Layout[$n]
  }
  $s  = [string]$c
  $ix = if ($Index.ContainsKey($c)) { $Index[$c] } else { -1 }
  if ($ShiftedSymbols.ContainsKey($s)) { return @{ Vk = [byte][char]$ShiftedSymbols[$s]; Shift = $true;  Idx = $ix } }
  if ($c -match '[0-9]')              { return @{ Vk = [byte][char]$c;                   Shift = $false; Idx = $ix } }
  if ($c -cmatch '[a-z]')             { return @{ Vk = [byte][char]$s.ToUpper();         Shift = $false; Idx = $ix } }
  if ($c -cmatch '[A-Z]')             { return @{ Vk = [byte][char]$c;                   Shift = $true;  Idx = $ix } }
  return $null
}

function Get-NoteName($k) {
  if ($k.Idx -lt 0) { return '?' }
  '{0}{1}' -f $NoteNames[$k.Idx % 12], ([Math]::Floor($k.Idx / 12) + 1)
}

# --- gather notes as {At, Dur, Key} -------------------------------------
$notes = New-Object System.Collections.ArrayList

if ($SelfTest) {
  # Every key once, slowly, whites then blacks, then mixed chords - so a key
  # that never sounds can be spotted by ear and by eye.
  $t = 0.0
  foreach ($c in $Layout.ToCharArray()) {
    $k = Resolve-Key $c
    if ($k) { [void]$notes.Add([pscustomobject]@{ At = $t; Dur = 320.0; Key = $k }); $t += 420.0 }
  }
  $t += 800.0
  foreach ($pair in @('el','uk','pl','ql','th','il')) {   # white+black together
    foreach ($c in $pair.ToCharArray()) {
      $k = Resolve-Key $c
      if ($k) { [void]$notes.Add([pscustomobject]@{ At = $t; Dur = 500.0; Key = $k }) }
    }
    $t += 700.0
  }
  $label = 'self test: every key, then mixed chords'
}

elseif ($Schedule) {
  foreach ($line in (Get-Content -LiteralPath $Schedule)) {
    if ($line -match '^\s*(#|$)') { continue }
    $f = $line -split "`t"
    if ($f.Count -lt 3) { continue }
    $k = Resolve-Key ([char]$f[2].Trim())
    if ($k) {
      [void]$notes.Add([pscustomobject]@{
        At  = [double]$f[0] / $Speed
        Dur = [double]$f[1] / $Speed
        Key = $k
      })
    }
  }
  $label = "schedule '$([IO.Path]::GetFileName($Schedule))' at speed $Speed"
}
else {
  if ($SheetFile) { $Sheet = Get-Content -Raw -LiteralPath $SheetFile }
  if (-not $Sheet) { throw "Give me -Schedule <file>, -SheetFile <file>, or -Sheet '<notes>'." }

  # A bar line and the spaces hugging it are one separator, not a rest.
  $Sheet = [regex]::Replace($Sheet, '[ \t]*\|[ \t]*', ' ')

  $groups = New-Object System.Collections.ArrayList
  $group  = New-Object System.Collections.ArrayList
  $i = 0
  while ($i -lt $Sheet.Length) {
    $c = $Sheet[$i]
    if ($c -eq '[') {
      $end = $Sheet.IndexOf(']', $i)
      if ($end -lt 0) { throw "Unclosed '[' at position $i." }
      $token = @()
      foreach ($ch in $Sheet.Substring($i + 1, $end - $i - 1).ToCharArray()) {
        $k = Resolve-Key $ch
        if ($k) { $token += $k }
      }
      if ($token.Count) { [void]$group.Add($token) }
      $i = $end + 1
    }
    elseif ([char]::IsWhiteSpace($c)) {
      if ($group.Count) { [void]$groups.Add($group.ToArray()); $group.Clear() }
      $spaces = 0; $newline = $false
      while ($i -lt $Sheet.Length -and [char]::IsWhiteSpace($Sheet[$i])) {
        if ($Sheet[$i] -eq "`n" -or $Sheet[$i] -eq "`r") { $newline = $true } else { $spaces++ }
        $i++
      }
      if (-not $newline) { for ($r = 1; $r -lt $spaces; $r++) { [void]$groups.Add(@()) } }
    }
    else {
      $k = Resolve-Key $c
      if ($k) { [void]$group.Add(@($k)) }
      $i++
    }
  }
  if ($group.Count) { [void]$groups.Add($group.ToArray()) }

  $span = 0.0
  foreach ($g in $groups) {
    if ($g.Count -eq 0) { $span += $UnitMs; continue }
    $slice = $UnitMs / [double]$g.Count
    foreach ($token in $g) {
      foreach ($k in $token) {
        [void]$notes.Add([pscustomobject]@{
          At = $span; Dur = [Math]::Min([double]$HoldMs, $slice * 0.9); Key = $k
        })
      }
      $span += $slice
    }
  }
  $label = "sheet, flat grid at $UnitMs ms/note"
}

if ($notes.Count -eq 0) { throw "Nothing to play." }

# Two players would fight over the same keyboard, so only one may run at a time.
$lockFile = Join-Path $PSScriptRoot '.playing.lock'
function Get-LivePlayer {
  if (-not (Test-Path $lockFile)) { return $null }
  try { $info = Get-Content -Raw -LiteralPath $lockFile | ConvertFrom-Json } catch { return $null }
  $p = Get-Process -Id $info.Pid -ErrorAction SilentlyContinue
  # The start time guards against a recycled process id.
  if ($p -and $p.StartTime.Ticks -eq $info.Start) { return $info }
  return $null
}

$busy = $(if ($DryRun) { $null } else { Get-LivePlayer })   # a dry run strikes nothing
if ($busy) {
  if (-not $Force) {
    Write-Host ''
    Write-Host ("  Already playing '{0}' (process {1})." -f $busy.Song, $busy.Pid) -ForegroundColor Red
    Write-Host '  Press END to stop that one, or rerun with -Force to take over.' -ForegroundColor DarkGray
    Write-Host ''
    return
  }
  Write-Host ("Stopping '{0}' (process {1})..." -f $busy.Song, $busy.Pid) -ForegroundColor Yellow
  if (-not ('RPKey.Tap' -as [type])) {
    Add-Type -Namespace RPKey -Name Tap -MemberDefinition @'
[DllImport("user32.dll")] public static extern void keybd_event(byte bVk, byte bScan, uint dwFlags, UIntPtr dwExtraInfo);
[DllImport("user32.dll")] public static extern uint MapVirtualKey(uint uCode, uint uMapType);
'@
  }
  $sc = [byte][RPKey.Tap]::MapVirtualKey(0x23, 0)
  [RPKey.Tap]::keybd_event(0x23, $sc, 0, [UIntPtr]::Zero)
  Start-Sleep -Milliseconds 150
  [RPKey.Tap]::keybd_event(0x23, $sc, 2, [UIntPtr]::Zero)
  for ($w = 0; $w -lt 30 -and (Get-LivePlayer); $w++) { Start-Sleep -Milliseconds 100 }
  if (Get-LivePlayer) {
    Get-Process -Id $busy.Pid -ErrorAction SilentlyContinue | Stop-Process -Force
    Start-Sleep -Milliseconds 300
  }
  Remove-Item $lockFile -ErrorAction SilentlyContinue
}

if (-not $DryRun) {
@{ Pid = $PID
   Start = (Get-Process -Id $PID).StartTime.Ticks
   Song = [IO.Path]::GetFileNameWithoutExtension($(if ($Schedule) { $Schedule } elseif ($SheetFile) { $SheetFile } else { 'sheet' }))
   When = (Get-Date).ToString('s')
} | ConvertTo-Json -Compress | Set-Content -LiteralPath $lockFile -Encoding ASCII
}

# --- collapse into batches of simultaneous key changes -------------------
$at = @{}
function Slot([double]$t) {
  $key = [Math]::Round($t, 0)
  if (-not $at.ContainsKey($key)) {
    $at[$key] = [pscustomobject]@{
      T = $key
      RelW = (New-Object System.Collections.ArrayList)
      RelB = (New-Object System.Collections.ArrayList)
      Wht = (New-Object System.Collections.ArrayList)
      Blk = (New-Object System.Collections.ArrayList)
    }
  }
  $at[$key]
}
foreach ($nt in $notes) {
  $on = Slot $nt.At
  if ($nt.Key.Shift) { [void]$on.Blk.Add($nt.Key.Vk) } else { [void]$on.Wht.Add($nt.Key.Vk) }
  $off = Slot ($nt.At + $nt.Dur)
  # A key must be lifted with Shift in the same state it was struck with.
  if ($nt.Key.Shift) { [void]$off.RelB.Add($nt.Key.Vk) } else { [void]$off.RelW.Add($nt.Key.Vk) }
}
$batches = $at.Values | Sort-Object T

$total = ($batches | Select-Object -Last 1).T
Write-Host ("{0}: {1} notes, {2} instants -> {3:mm\:ss}" -f `
  $label, $notes.Count, $batches.Count, [timespan]::FromMilliseconds($total)) -ForegroundColor Cyan

if ($DryRun) {
  $n = 0
  foreach ($b in $batches) {
    if ($b.Wht.Count -eq 0 -and $b.Blk.Count -eq 0) { continue }
    $hit = @($notes | Where-Object { [Math]::Round($_.At,0) -eq $b.T } | ForEach-Object { Get-NoteName $_.Key })
    Write-Host ("{0,8} ms   {1}" -f $b.T, ($hit -join '+'))
    if (++$n -ge 20) { Write-Host "  ... ($($batches.Count - 20) more instants)"; break }
  }
  return
}

$src  = Get-Content -Raw -LiteralPath (Join-Path $PSScriptRoot 'Engine.cs')
$md5  = [Security.Cryptography.MD5]::Create()
$tag  = ([BitConverter]::ToString($md5.ComputeHash([Text.Encoding]::UTF8.GetBytes($src)))).Replace('-','').Substring(0,8)
$name = "PianoEngine_$tag"
if (-not ($name -as [type])) {
  Add-Type -TypeDefinition ($src.Replace('class PianoEngine', "class $name")) -ErrorAction Stop
}
$Engine = $name -as [type]

$n       = $batches.Count
$pressAt = New-Object 'double[]'  $n
$relWArr = New-Object 'byte[][]'  $n
$relBArr = New-Object 'byte[][]'  $n
$whtArr  = New-Object 'byte[][]'  $n
$blkArr  = New-Object 'byte[][]'  $n
$i = 0
foreach ($b in $batches) {
  $pressAt[$i] = [double]$b.T
  $relWArr[$i] = [byte[]]$b.RelW.ToArray()
  $relBArr[$i] = [byte[]]$b.RelB.ToArray()
  $whtArr[$i]  = [byte[]]$b.Wht.ToArray()
  $blkArr[$i]  = [byte[]]$b.Blk.ToArray()
  $i++
}

try {

$proc = Get-Process | Where-Object { $_.MainWindowTitle -match 'Roblox' } | Select-Object -First 1
if ($proc) { [void]$Engine::Focus($proc.MainWindowHandle) }

for ($t = $Countdown; $t -gt 0; $t--) {
  Write-Host "Click the Roblox piano... starting in $t   (END = stop, F8 = pause)" -ForegroundColor Yellow
  Start-Sleep -Seconds 1
}

$Engine::Play($pressAt, $relWArr, $relBArr, $whtArr, $blkArr, $NoInput.IsPresent, (-not $NoFocusGuard.IsPresent), 150)

$err    = $Engine::Err
$played = $Engine::Played
$paused = $Engine::PausedMs
$took   = $Engine::TotalMs

if ($Trace) {
  $lines = New-Object System.Collections.ArrayList
  for ($i = 0; $i -lt $played; $i++) { [void]$lines.Add(("{0}`t{1}" -f $pressAt[$i], ($pressAt[$i] + $err[$i]))) }
  $lines | Set-Content -LiteralPath $Trace -Encoding ASCII
}

$byKey   = $Engine::KeyPausedMs
$byFocus = $Engine::FocusPausedMs
if ($byKey   -gt 0) { Write-Host ("Note: held {0:n1}s on F8." -f ($byKey   / 1000)) -ForegroundColor DarkYellow }
if ($byFocus -gt 0) { Write-Host ("Note: held {0:n1}s while Roblox was not focused." -f ($byFocus / 1000)) -ForegroundColor DarkYellow }
if ($Engine::Aborted) {
  Write-Host ("Aborted after {0}/{1} instants." -f $played, $n) -ForegroundColor Red
} else {
  $abs   = @(for ($i = 0; $i -lt $played; $i++) { [Math]::Abs($err[$i]) })
  $worst = ($abs | Measure-Object -Maximum).Maximum
  $mean  = ($abs | Measure-Object -Average).Average
  Write-Host ("Done. {0}/{1} instants in {2:mm\:ss}  |  timing error: mean {3:n2} ms, worst {4:n2} ms" -f `
    $played, $n, [timespan]::FromMilliseconds($took), $mean, $worst) -ForegroundColor Green
}

} finally {
  # Whatever happened, let the next run start.
  Remove-Item -LiteralPath $lockFile -ErrorAction SilentlyContinue
}
