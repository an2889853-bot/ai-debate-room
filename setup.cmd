@echo off
rem AI Debate Room - one-click setup. Runs tools\setup.ps1 (installs uv/Python, venv, packages, both CLIs, then logins).
rem Keep this file ASCII-only (cmd.exe reads batch files in the system code page).
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0tools\setup.ps1"
