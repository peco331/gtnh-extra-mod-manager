@echo off
rem 本地打包：生成 dist\GTNHModManager.exe（GUI）与 dist\gtnh-cli.exe（命令行）
rem 依赖：py -m pip install pyinstaller curl_cffi
cd /d "%~dp0.."
py -m PyInstaller --onefile --windowed --name GTNHModManager ^
    --collect-all curl_cffi launcher_gui.py || goto :err
py -m PyInstaller --onefile --console --name gtnh-cli ^
    --collect-all curl_cffi launcher_cli.py || goto :err
echo.
echo 构建完成：dist\GTNHModManager.exe 与 dist\gtnh-cli.exe
exit /b 0
:err
echo 构建失败
exit /b 1
