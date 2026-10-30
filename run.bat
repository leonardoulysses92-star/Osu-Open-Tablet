@echo off
rem Launches the assistant with the Python launcher (no installation needed).
py -3 -m osu_tablet_assistant %*
if errorlevel 1 pause
