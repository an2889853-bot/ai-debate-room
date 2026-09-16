@echo off
rem AI Debate Room - start the Streamlit UI in this window (no venv activation needed).
rem Keep this file ASCII-only (cmd.exe reads batch files in the system code page).
cd /d "%~dp0"
".venv\Scripts\python.exe" -m streamlit run app.py --browser.gatherUsageStats false
