@echo off
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  python -m venv .venv
  ".venv\Scripts\python.exe" -m pip install -r requirements.txt
)
start "" http://localhost:8770/hub.html
".venv\Scripts\python.exe" hub\server.py
