@echo off
chcp 65001 >nul
cd /d "%~dp0"
set PY=python
where python >nul 2>nul
if errorlevel 1 set PY=py
%PY% -V >nul 2>nul
if errorlevel 1 (
  echo [ERROR] Python 3 not found. Please install it from https://www.python.org/
  pause
  exit /b 1
)
%PY% -c "import fontTools, shapely" >nul 2>nul
if errorlevel 1 (
  echo Installing dependencies: fonttools shapely ...
  %PY% -m pip install -q fonttools shapely
)
echo CharStrokeLab starting... close this window to stop the server.
set ROOT=%~dp0.
if not exist "%ROOT%\Fonts" if exist "%~dp0..\Fonts" set ROOT=%~dp0..
%PY% -m strokelab.server --root "%ROOT%" --open
