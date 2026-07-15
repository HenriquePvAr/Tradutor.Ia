@echo off
REM Launch the local Tradutor.Ia system: independent worker + UI.
REM Usage: start_tradutor.bat [all|worker|ui|status|stop]
setlocal
set "HERE=%~dp0"
set "PY=%HERE%.venv\Scripts\python.exe"
if not exist "%PY%" set "PY=python"
"%PY%" "%HERE%start_tradutor.py" %*
endlocal
