@echo off
cd /d "%~dp0"

echo Starting PolyEdge...

:: Install dependencies if needed
pip show flask >nul 2>&1
if errorlevel 1 (
    echo Installing dependencies...
    pip install -r requirements.txt
)

:: Start the Flask server
start "" /B py server.py

:: Wait a moment then open in browser
timeout /t 3 /nobreak > nul
start "" "http://localhost:5001"
