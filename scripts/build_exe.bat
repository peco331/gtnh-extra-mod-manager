@echo off
rem 本地打包：生成 dist\GTNHModManager.exe（GUI）与 dist\gtnh-cli.exe（命令行）
rem 依赖：py -3.10 -m pip install pyinstaller curl_cffi websocket-client
cd /d "%~dp0.."
set "PYTHON_EXE=py"
set "PYTHON_VERSION=-3.10"
if not defined pythonLocation goto :python_ready
set "PYTHON_EXE=%pythonLocation%\python.exe"
set "PYTHON_VERSION="
:python_ready
set "WS_HIDDEN=--hidden-import websocket --hidden-import websocket._abnf --hidden-import websocket._app --hidden-import websocket._cookiejar --hidden-import websocket._core --hidden-import websocket._exceptions --hidden-import websocket._handshake --hidden-import websocket._http --hidden-import websocket._logging --hidden-import websocket._socket --hidden-import websocket._ssl_compat --hidden-import websocket._url --hidden-import websocket._utils --hidden-import websocket._wsdump"
"%PYTHON_EXE%" %PYTHON_VERSION% -m PyInstaller --onefile --windowed --name GTNHModManager ^
    --collect-all curl_cffi %WS_HIDDEN% launcher_gui.py || goto :err
"%PYTHON_EXE%" %PYTHON_VERSION% -m PyInstaller --onefile --console --name gtnh-cli ^
    --collect-all curl_cffi %WS_HIDDEN% launcher_cli.py || goto :err
echo.
echo 构建完成：dist\GTNHModManager.exe 与 dist\gtnh-cli.exe
exit /b 0
:err
echo 构建失败
exit /b 1
