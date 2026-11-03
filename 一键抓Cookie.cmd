@echo off
chcp 65001 >nul
cd /d "%~dp0"

net session >nul 2>&1
if errorlevel 1 (
  echo 正在请求管理员权限...
  powershell -NoProfile -ExecutionPolicy Bypass -Command "Start-Process -FilePath '%~f0' -WorkingDirectory '%~dp0' -Verb RunAs"
  exit /b
)

echo ============================================================
echo   苹果抢购助手 - 一键抓 PC 微信 Cookie
echo ============================================================
echo.
echo 开始前请确认：
echo 1. PC 微信已经登录要抓的账号。
echo 2. 已在 PC 微信里打开苹果小程序，并进入过购物袋页面。
echo 3. 抓取期间不要关闭微信。
echo.

if exist "%~dp0抓Cookie-PC.exe" (
  "%~dp0抓Cookie-PC.exe" %*
  set "RC=%ERRORLEVEL%"
  goto :done
)

where python >nul 2>&1
if errorlevel 1 (
  echo [错误] 没有找到 Python，也没有找到抓Cookie-PC.exe。
  echo 请使用分享包自带的一键程序，或安装 Python 3.10 以上版本。
  set "RC=10"
  goto :done
)

python -c "import curl_cffi" >nul 2>&1
if errorlevel 1 (
  echo [提示] 正在安装抓 Cookie 所需依赖 curl_cffi...
  python -m pip install -U curl_cffi
  if errorlevel 1 (
    echo [错误] curl_cffi 安装失败，请检查网络或 Python 环境。
    set "RC=11"
    goto :done
  )
)

python "%~dp0抓身份-PC.py" %*
set "RC=%ERRORLEVEL%"

:done
echo.
if "%RC%"=="0" (
  echo 抓 Cookie 已结束。请到分享包根目录的“身份”文件夹查看新文件。
) else (
  echo 抓 Cookie 未完全成功，退出码=%RC%。请对照“抓Cookie-先看我.txt”排查。
)
echo.
pause
exit /b %RC%
