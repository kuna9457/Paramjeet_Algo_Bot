@echo off
rem Release Admin: bump version, build the installer
cd /d "%~dp0"
start "" pythonw release_admin.py
