$ErrorActionPreference='Stop'
$root=Split-Path -Parent $PSScriptRoot
$build='D:\素材采集\studio\build'
$release='D:\素材采集\studio\release\墨流-0.1.0'
if([IO.Path]::GetFullPath($release) -ne 'D:\素材采集\studio\release\墨流-0.1.0'){throw 'Release path mismatch'}
if(Get-Process -Name moflow,moflow-service -ErrorAction SilentlyContinue){throw '请先退出墨流，再更新交付文件'}
New-Item -ItemType Directory -Path $release,(Join-Path $release 'licenses') -Force|Out-Null
foreach($binary in @("$build\target\release\moflow.exe","$build\python\moflow-service\moflow-service.exe")){
 if(!(Test-Path -LiteralPath $binary)){throw "Missing binary: $binary"}
 Copy-Item -LiteralPath $binary -Destination $release -Force
}
& robocopy "$build\python\moflow-service\_internal" "$release\_internal" /E /NFL /NDL /NJH /NJS /NP | Out-Null
if($LASTEXITCODE -ge 8){throw 'Sidecar copy failed'}
& robocopy "$root\dist" "$release\_internal\dist" /E /NFL /NDL /NJH /NJS /NP | Out-Null
if($LASTEXITCODE -ge 8){throw 'Frontend copy failed'}
foreach($name in @('README.md','SOURCES.md','测试报告.md','功能对齐.md','爬取方案对比.md','自动生产与远程开发说明.md','聊天与插件说明.md','创作中心改版说明.md','搜索与推荐说明.md')){
 if(Test-Path -LiteralPath (Join-Path $root $name)){Copy-Item -LiteralPath (Join-Path $root $name) -Destination $release -Force}
}
Copy-Item -LiteralPath "$root\vendor\ASCII-LICENSE.txt","$root\vendor\F2-LICENSE.txt","$root\package-lock.json","$root\uv.lock","$root\src-tauri\Cargo.lock" -Destination "$release\licenses" -Force
New-Item -ItemType Directory -Path "$release\licenses\dtk" -Force|Out-Null
Copy-Item -LiteralPath "$root\vendor\DTK-LICENSE.txt" -Destination "$release\licenses\dtk\LICENSE" -Force
Copy-Item -LiteralPath "$root\vendor\DTK-NOTICE.txt" -Destination "$release\licenses\dtk\NOTICE" -Force
Copy-Item -LiteralPath "$root\vendor\dtk-source.json" -Destination "$release\licenses\dtk\source.json" -Force
if(Test-Path -LiteralPath 'D:\素材采集\studio\third-party'){
 & robocopy 'D:\素材采集\studio\third-party' "$release\licenses\python" /E /NFL /NDL /NJH /NJS /NP | Out-Null
 if($LASTEXITCODE -ge 8){throw 'License copy failed'}
}
$ffmpeg=(Get-Command ffmpeg).Source
$ffmpegRoot=Split-Path -Parent (Split-Path -Parent $ffmpeg)
Copy-Item -LiteralPath (Join-Path $ffmpegRoot 'LICENSE') -Destination "$release\licenses\FFmpeg-GPL.txt" -Force
& $ffmpeg -L 2>&1 | Set-Content -LiteralPath "$release\licenses\FFmpeg-build.txt" -Encoding utf8
$shell=New-Object -ComObject WScript.Shell
foreach($shortcut in @("$root\启动墨流.lnk",(Join-Path ([Environment]::GetFolderPath('Desktop')) '墨流 Studio.lnk'))){
 $link=$shell.CreateShortcut($shortcut);$link.TargetPath="$release\moflow.exe";$link.WorkingDirectory=$release;$link.IconLocation="$release\moflow.exe,0";$link.Description='墨流：本地素材与 AI 自动化工作台';$link.Save()
}
$stop=$shell.CreateShortcut("$root\停止墨流.lnk");$stop.TargetPath="$release\moflow.exe";$stop.Arguments='--quit';$stop.WorkingDirectory=$release;$stop.Save()
Get-FileHash -LiteralPath "$release\moflow.exe","$release\moflow-service.exe" -Algorithm SHA256 | Select-Object Path,Hash | ConvertTo-Json | Set-Content -LiteralPath "$release\manifest.json" -Encoding utf8
Write-Output $release
