@echo off
rem Double-click this file on Windows to open PDF Scrubber when running from the source files.
rem (The installer from the Releases page needs none of this.)
title PDF Scrubber
cd /d "%~dp0"

where py >nul 2>nul
if not errorlevel 1 (
    py scrubber_gui.py
    goto done
)
where python >nul 2>nul
if not errorlevel 1 (
    python scrubber_gui.py
    goto done
)

echo Python is not installed on this computer.
echo.
echo 1. Go to https://www.python.org/downloads/ and download Python.
echo 2. Run the installer and tick "Add python.exe to PATH" on the first screen.
echo 3. Double-click this file again.

:done
echo.
pause
