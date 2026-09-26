@echo off
rem SearXNG inditasa + grafikus kezelo (Windows). Ha nincs telepitve, telepiti.
chcp 65001 >nul
cd /d "%~dp0"
set "PY="
where py >nul 2>nul && set "PY=py -3"
if not defined PY (
  where python >nul 2>nul && python -c "import sys; sys.exit(0 if sys.version_info>=(3,9) else 1)" >nul 2>nul && set "PY=python"
)
if not defined PY (
  echo Python 3.9+ nem talalhato.
  where winget >nul 2>nul && (
    echo Telepites winget-tel: Python 3.12 ...
    winget install -e --id Python.Python.3.12 --accept-package-agreements --accept-source-agreements
    echo.
    echo Zard be ezt az ablakot, majd inditsd ujra a start.bat-ot.
    pause
    exit /b 1
  )
  echo Telepitsd innen: https://www.python.org/downloads/  ^(pipald be: "Add python.exe to PATH"^)
  pause
  exit /b 1
)
%PY% manager.py gui --autostart %*
if errorlevel 1 (
  echo.
  echo A SearXNG kezelo hibaval allt le - a fenti uzenet mutatja az okat.
  pause
)
