$ErrorActionPreference='Stop'
$root=Split-Path -Parent $PSScriptRoot
$buildRoot='D:\素材采集\studio'
$env:RUSTUP_HOME="$buildRoot\rustup"
$env:CARGO_HOME="$buildRoot\cargo"
$env:CARGO_TARGET_DIR="$buildRoot\build\target"
$env:CARGO_BUILD_JOBS='2'
$env:TEMP="$buildRoot\tmp"
$env:TMP=$env:TEMP
New-Item -ItemType Directory -Path $env:TEMP -Force|Out-Null
$vc='C:\Program Files\Microsoft Visual Studio\2022\Community\VC\Tools\MSVC\14.44.35207'
$sdk="$buildRoot\sdk\microsoft.windows.sdk.cpp\c"
$lib="$buildRoot\sdk\microsoft.windows.sdk.cpp.x64\c"
$env:PATH="$buildRoot\cargo\bin;$vc\bin\Hostx64\x64;$sdk\bin\10.0.26100.0\x64;"+$env:PATH
$env:LIB="$vc\lib\x64;$lib\um\x64;$lib\ucrt\x64"
$env:INCLUDE="$vc\include;$sdk\Include\10.0.26100.0\um;$sdk\Include\10.0.26100.0\shared;$sdk\Include\10.0.26100.0\ucrt;$sdk\Include\10.0.26100.0\winrt"
$env:WindowsSdkDir="$sdk\"
$env:WindowsSDKVersion='10.0.26100.0\'
$env:RC="$sdk\bin\10.0.26100.0\x64\rc.exe"
Set-Location -LiteralPath $root
npm run tauri -- build --no-bundle
if($LASTEXITCODE){throw 'Desktop build failed'}
