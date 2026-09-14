@echo off
rem run_tests.bat - one-click gate suite (double-click = std).
rem This file must stay pure ASCII: on cp936 consoles cmd mis-parses
rem multi-byte UTF-8 comment lines even after chcp 65001 (verified on
rem this machine), so all Chinese output lives in tools/gate_report.py.
rem chcp 65001 below only makes the python tool's UTF-8 output readable.
rem Usage: run_tests.bat [quick^|std^|full] [extra gate_report.py args]
rem   quick = bench + parity + sample      (~25 min)
rem   std   = quick + cross + coverage     (default)
rem   full  = std + full-library + trace   (hours, work machine)
rem Exit code passthrough: 0 = all hard gates pass, 1 = fail, 2 = bad args.
setlocal EnableDelayedExpansion
chcp 65001 >nul
cd /d "%~dp0"

set "SUITE=%~1"
if "%SUITE%"=="" set "SUITE=std"

python -X utf8 "tools\gate_report.py" --suite %SUITE% %2 %3 %4 %5
set "RC=%ERRORLEVEL%"

echo.
if exist "verifyOut\gateReport\latest.txt" (
    set /p GATE_DIR=<"verifyOut\gateReport\latest.txt"
    echo Report dir : !GATE_DIR!
    echo Result file: !GATE_DIR!\report.json
)
if "%RC%"=="0" (echo GATE: ALL PASS) else (echo GATE: FAIL or ERROR, exit code %RC%)

rem Pause only when double-clicked; set GATE_NO_PAUSE=1 to skip in scripts.
if defined GATE_NO_PAUSE goto :finish
echo %CMDCMDLINE% | findstr /i /c:"%~nx0" >nul && pause
:finish
exit /b %RC%
