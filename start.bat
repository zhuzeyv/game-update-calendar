@echo off
setlocal
cd /d "%~dp0"

set "PY="
for /d %%D in ("%USERPROFILE%\.dsh\dsh-runtimes\*") do (
  if exist "%%~fD\dependencies\python\python.exe" set "PY=%%~fD\dependencies\python\python.exe"
)

if defined PY (
  "%PY%" server.py --open
  goto :end
)

where python >nul 2>nul
if errorlevel 1 (
  echo.
  echo Python 3 was not found.
  echo Install Python 3.10+ or run server.py with an existing Python interpreter.
  echo.
  pause
  exit /b 1
)

python server.py --open

:end
if errorlevel 1 pause
endlocal
