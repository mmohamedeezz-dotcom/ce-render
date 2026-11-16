# العقل المدبر — runs on the office PC: downloads the audio, then only the shortlisted parts.
param([string]$mode)
$ProgressPreference = 'SilentlyContinue'
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
$t = "$env:USERPROFILE\ce-tools"
New-Item -ItemType Directory -Force $t | Out-Null
if (!(Test-Path "$t\yt-dlp.exe")) { Invoke-WebRequest https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp.exe -OutFile "$t\yt-dlp.exe" }
if (!(Test-Path "$t\ffmpeg.exe")) {
  Invoke-WebRequest https://github.com/yt-dlp/FFmpeg-Builds/releases/latest/download/ffmpeg-master-latest-win64-gpl.zip -OutFile "$t\ff.zip"
  Expand-Archive "$t\ff.zip" "$t\ff" -Force
  Copy-Item "$t\ff\*\bin\ff*.exe" $t
  Remove-Item "$t\ff.zip"
}
if (!(Test-Path "$t\deno.exe")) {
  Invoke-WebRequest https://github.com/denoland/deno/releases/latest/download/deno-x86_64-pc-windows-msvc.zip -OutFile "$t\deno.zip"
  Expand-Archive "$t\deno.zip" $t -Force
  Remove-Item "$t\deno.zip"
}
$env:PATH = "$t;$env:PATH"
$yt = "$t\yt-dlp.exe"
$url = $env:URL
if (!$url) { Write-Host "no URL"; exit 1 }

if ($mode -eq 'audio') {
  & $yt -U | Out-Null
  Remove-Item -Force audio.* -ErrorAction SilentlyContinue
  & $yt -f "ba/b" -x --audio-format mp3 --audio-quality 64K --postprocessor-args "ffmpeg:-ac 1 -ar 16000" --no-playlist -o "audio.%(ext)s" $url
  if (!(Test-Path audio.mp3)) { Write-Host "audio download failed"; exit 1 }
  Get-Item audio.mp3 | Format-Table Name, Length
  exit 0
}

if ($mode -eq 'clips') {
  $c = Get-Content brain\cands.json -Raw -Encoding UTF8 | ConvertFrom-Json
  Remove-Item -Recurse -Force clips -ErrorAction SilentlyContinue
  New-Item -ItemType Directory -Force clips | Out-Null
  $inv = [cultureinfo]::InvariantCulture
  $procs = @()
  foreach ($x in $c) {
    $sec = [string]::Format($inv, "*{0}-{1}", $x.start, $x.end)
    $a = @('-f', '"bv*[height<=1080][vcodec^=avc1]+ba/bv*[height<=1080]+ba/b"', '--download-sections', "`"$sec`"",
           '--force-keyframes-at-cuts', '--merge-output-format', 'mp4', '--no-playlist', '--no-progress',
           '-o', "`"clips/c$($x.id).%(ext)s`"", "`"$url`"")
    $procs += Start-Process -FilePath $yt -ArgumentList $a -NoNewWindow -PassThru
    if ($procs.Count % 4 -eq 0) { $procs | Wait-Process -ErrorAction SilentlyContinue }
  }
  $procs | Wait-Process -ErrorAction SilentlyContinue
  Get-ChildItem clips | Format-Table Name, Length
  if (!(Get-ChildItem clips\*.mp4)) { Write-Host "no clips downloaded"; exit 1 }
  exit 0
}
Write-Host "unknown mode"; exit 1
