<#
  Song picker for the Roblox piano player.

    .\Play.ps1                    pick from a menu
    .\Play.ps1 -Song river        play the first song matching "river"
    .\Play.ps1 -Speed 0.9         any Play-Piano option is passed through

  Songs are the .sched files in the songs folder. Add one with Add-Song.ps1.
#>
param(
  [string]$Song,
  [double]$Speed = 1.0,
  [int]$Countdown = 5,
  [switch]$DryRun,
  [switch]$NoInput,
  [switch]$NoFocusGuard
)

$ErrorActionPreference = 'Stop'
$songDir = Join-Path $PSScriptRoot 'songs'
if (-not (Test-Path $songDir)) { throw "No songs folder at $songDir" }

# Read each schedule's header and work out how long it runs.
$list = @()
foreach ($f in (Get-ChildItem -LiteralPath $songDir -Filter '*.sched' | Sort-Object Name)) {
  $title = [IO.Path]::GetFileNameWithoutExtension($f.Name)
  $artist = ''
  $notes = 0
  $end = 0.0
  foreach ($line in [IO.File]::ReadLines($f.FullName)) {
    if ($line.StartsWith('#')) {
      if ($line -match '^#\s*title:\s*(.+)$')  { $title  = $Matches[1].Trim() }
      if ($line -match '^#\s*artist:\s*(.+)$') { $artist = $Matches[1].Trim() }
      continue
    }
    if (-not $line.Trim()) { continue }
    $p = $line -split "`t"
    if ($p.Count -ge 3) {
      $notes++
      $t = [double]$p[0] + [double]$p[1]
      if ($t -gt $end) { $end = $t }
    }
  }
  $list += [pscustomobject]@{
    File = $f.FullName; Key = [IO.Path]::GetFileNameWithoutExtension($f.Name)
    Title = $title; Artist = $artist; Notes = $notes
    Length = [timespan]::FromMilliseconds($end)
  }
}
if ($list.Count -eq 0) { throw "No .sched files in $songDir. Add one with Add-Song.ps1." }

function Show-Menu {
  Write-Host ''
  Write-Host '  Roblox Piano' -ForegroundColor Cyan
  Write-Host '  ------------' -ForegroundColor DarkGray
  for ($i = 0; $i -lt $list.Count; $i++) {
    $s = $list[$i]
    Write-Host ('  {0,2}. ' -f ($i + 1)) -NoNewline -ForegroundColor Yellow
    # Long titles and artists are trimmed so the columns never collide.
    $title  = if ($s.Title.Length  -gt 27) { $s.Title.Substring(0,26)  + '.' } else { $s.Title }
    $artist = if ($s.Artist.Length -gt 23) { $s.Artist.Substring(0,22) + '.' } else { $s.Artist }
    Write-Host ('{0,-28}' -f $title) -NoNewline
    Write-Host ('{0,-24}' -f $artist) -NoNewline -ForegroundColor DarkGray
    Write-Host ('{0:mm\:ss}  {1,4} notes' -f $s.Length, $s.Notes) -ForegroundColor DarkGray
  }
  Write-Host ('   q. quit') -ForegroundColor DarkGray
  Write-Host ''
}

$pick = $null
if ($Song) {
  $pick = $list | Where-Object { $_.Key -like "*$Song*" -or $_.Title -like "*$Song*" } | Select-Object -First 1
  if (-not $pick) { throw "No song matching '$Song'. Run .\Play.ps1 with no arguments to see the list." }
}
else {
  while ($true) {
    Show-Menu
    $answer = Read-Host '  Play which one'
    if ($answer -in 'q','Q','') { Write-Host '  Nothing played.' -ForegroundColor DarkGray; return }
    $n = 0
    if ([int]::TryParse($answer, [ref]$n) -and $n -ge 1 -and $n -le $list.Count) { $pick = $list[$n - 1]; break }
    $m = $list | Where-Object { $_.Key -like "*$answer*" -or $_.Title -like "*$answer*" } | Select-Object -First 1
    if ($m) { $pick = $m; break }
    Write-Host "  '$answer' is not on the list." -ForegroundColor Red
  }
}

Write-Host ''
Write-Host ("  Playing: {0}{1}  ({2:mm\:ss})" -f $pick.Title, $(if ($pick.Artist) { " - $($pick.Artist)" } else { '' }), $pick.Length) -ForegroundColor Green
Write-Host '  END or Ctrl+C = stop, F8 = pause' -ForegroundColor DarkGray
Write-Host ''

$args = @{ Schedule = $pick.File; Speed = $Speed; Countdown = $Countdown }
if ($DryRun)       { $args.DryRun = $true }
if ($NoInput)      { $args.NoInput = $true }
if ($NoFocusGuard) { $args.NoFocusGuard = $true }
& (Join-Path $PSScriptRoot 'Play-Piano.ps1') @args
