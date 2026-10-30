@echo off
rem Launches the assistant with the Python launcher (no installation needed).
rem "-m" finds the package in the current folder, so start from this file's folder.
cd /d "%~dp0"
py -3 -m osu_tablet_assistant %*
if errorlevel 1 pause
