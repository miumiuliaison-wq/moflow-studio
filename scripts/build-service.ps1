$ErrorActionPreference='Stop'
$root=Split-Path -Parent $PSScriptRoot
$work='D:\素材采集\studio\build'
$env:TEMP='D:\素材采集\studio\tmp'
$env:TMP=$env:TEMP
New-Item -ItemType Directory -Path $env:TEMP -Force|Out-Null
if([IO.Path]::GetFullPath($work) -ne 'D:\素材采集\studio\build'){throw 'Build path verification failed'}
New-Item -ItemType Directory -Path $work,(Join-Path $root 'src-tauri\binaries'),(Join-Path $root 'src-tauri\resources'),(Join-Path $root 'media-tools') -Force | Out-Null
foreach($tool in @('ffprobe','ffmpeg')){Copy-Item -LiteralPath (Get-Command $tool).Source -Destination (Join-Path $root "media-tools\$tool.exe") -Force}
Set-Location -LiteralPath $root
& "$root\.venv\Scripts\python.exe" -m PyInstaller --noconfirm --onedir --name moflow-service --distpath "$work\python" --workpath "$work\pyinstaller" --specpath "$work" --add-data "$root\dist;dist" --add-data "$root\backend\contracts.json;backend" --add-data "$root\media-tools;media-tools" --collect-all f2 --collect-all playwright --collect-all patchright --collect-submodules langgraph --collect-submodules langchain_core --collect-submodules mcp.server --collect-submodules backend service.py
if($LASTEXITCODE){throw 'Python service build failed'}
Copy-Item -LiteralPath "$work\python\moflow-service\moflow-service.exe" -Destination "$root\src-tauri\binaries\moflow-service-x86_64-pc-windows-msvc.exe" -Force
if(Test-Path -LiteralPath "$root\src-tauri\resources\_internal"){
 $existing=Get-Item -LiteralPath "$root\src-tauri\resources\_internal"
 if(!$existing.LinkType -or $existing.Target -ne "$work\python\moflow-service\_internal"){throw 'Build resource path mismatch'}
}else{New-Item -ItemType Junction -Path "$root\src-tauri\resources\_internal" -Target "$work\python\moflow-service\_internal" | Out-Null}
