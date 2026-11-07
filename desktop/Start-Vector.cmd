@echo off
setlocal EnableExtensions DisableDelayedExpansion
rem Native source entry only. Never edits backend configuration or stored data.
pushd "%~dp0"
if errorlevel 1 (
  echo Cannot open the desktop source directory.
  if not defined CI pause
  exit /b 1
)

set "VECTOR_ENV_PY=.venv\Scripts\python.exe"
set "VECTOR_PY="
set "VECTOR_PY_OPTION="
if exist "%VECTOR_ENV_PY%" goto :check_env
if exist ".venv" (
  echo The existing .venv is incomplete or belongs to another operating system.
  echo Preserve your files and rename that directory before creating a Windows environment.
  goto :failed
)

where py.exe >nul 2>&1
if errorlevel 1 goto :find_python
py -3.12 -c "import struct,sys; raise SystemExit(not ((3,10) <= sys.version_info[:2] <= (3,14) and struct.calcsize('P') == 8))" >nul 2>&1
if errorlevel 1 goto :try_default_launcher
set "VECTOR_PY=py.exe"
set "VECTOR_PY_OPTION=-3.12"
goto :make_env

:try_default_launcher
py -3 -c "import struct,sys; raise SystemExit(not ((3,10) <= sys.version_info[:2] <= (3,14) and struct.calcsize('P') == 8))" >nul 2>&1
if errorlevel 1 goto :find_python
set "VECTOR_PY=py.exe"
set "VECTOR_PY_OPTION=-3"
goto :make_env

:find_python
rem Skip WindowsApps execution aliases instead of opening the Microsoft Store.
for /f "delims=" %%P in ('where python.exe 2^>nul ^| findstr /i /l /v /c:"\Microsoft\WindowsApps"') do (
  "%%P" -c "import struct,sys; raise SystemExit(not ((3,10) <= sys.version_info[:2] <= (3,14) and struct.calcsize('P') == 8))" >nul 2>&1
  if not errorlevel 1 (
    set "VECTOR_PY=%%P"
    goto :make_env
  )
)
echo A working 64-bit Python 3.10 to 3.14 installation is required for source mode.
echo Install Python 3.12 from python.org or use the packaged Windows application.
goto :failed

:make_env
"%VECTOR_PY%" %VECTOR_PY_OPTION% -m venv .venv
if errorlevel 1 goto :failed

:check_env
"%VECTOR_ENV_PY%" -c "import struct,sys; raise SystemExit(not ((3,10) <= sys.version_info[:2] <= (3,14) and struct.calcsize('P') == 8))" >nul 2>&1
if errorlevel 1 (
  echo The local virtual environment needs 64-bit Python 3.10 to 3.14.
  goto :failed
)
"%VECTOR_ENV_PY%" -m pip install --disable-pip-version-check -r requirements.txt
if errorlevel 1 goto :failed
"%VECTOR_ENV_PY%" run.py %*
if errorlevel 1 goto :failed
popd
exit /b 0

:failed
set "VECTOR_EXIT=%ERRORLEVEL%"
if "%VECTOR_EXIT%"=="0" set "VECTOR_EXIT=1"
echo.
echo Vector did not start successfully. Keep the error text above for diagnosis.
echo Your backend configuration and existing data have not been reset.
if not defined CI pause
popd
exit /b %VECTOR_EXIT%
