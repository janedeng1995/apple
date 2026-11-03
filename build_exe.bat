@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo ============================================================
echo   AppleMonitorPro  ·  打包成 exe
echo ============================================================
echo.
echo [1/3] 安装依赖...
python -m pip install -r requirements.txt -U pyinstaller
if errorlevel 1 ( echo 依赖安装失败 & pause & exit /b 1 )
echo.
echo [2/3] 运行核心回归测试...
set "PYTHONPATH=%CD%"
python -m unittest discover -s ..\tests -v
if errorlevel 1 ( echo 测试失败，已停止打包 & pause & exit /b 1 )
echo.
echo [3/3] 打包（单文件、不弹黑框）...
python -m PyInstaller --noconfirm --clean --onefile --windowed ^
  --name AppleMonitorPro ^
  --add-data "config.example.json;." ^
  --collect-submodules apple_monitor ^
  --collect-all curl_cffi ^
  --collect-all ttkbootstrap ^
  run.py
if errorlevel 1 ( echo 打包失败 & pause & exit /b 1 )
echo.
echo ============================================================
echo   完成！exe 在  dist\AppleMonitorPro.exe
echo   首次运行会在 exe 同目录生成 config.json（从模板复制）
echo ============================================================
pause
