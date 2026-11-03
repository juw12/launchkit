@echo off
cd /d "%~dp0"
if not exist .env echo No .env found - copy .env.example to .env to configure email and AI.
python launchkit.py
pause
