@echo off
rem AI Debate Room launcher (used by the desktop shortcut).
rem NOTE: keep this file ASCII-only. cmd.exe reads batch files in the system
rem code page (CP949 on Korean Windows), so UTF-8 Korean comments break parsing.
rem - If the server is already listening on 8501, just open the browser.
rem - Otherwise start the server in a minimized window (it opens the browser itself).
cd /d "%~dp0"
netstat -ano | findstr /C:":8501 " | findstr /C:"LISTENING" >nul
if %errorlevel%==0 (
    start "" http://localhost:8501
) else (
    start "AI Debate Room server" /min cmd /c "".venv\Scripts\python.exe" -m streamlit run app.py --browser.gatherUsageStats false"
)
