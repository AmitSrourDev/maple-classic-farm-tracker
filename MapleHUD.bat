@echo off
rem Run the overlay from source. Installed users: use the Start menu shortcut instead.
cd /d "%~dp0"
if not exist ".venv\Scripts\pythonw.exe" (
    py -3 -m venv .venv 2>nul || python -m venv .venv
    ".venv\Scripts\python.exe" -m pip install --upgrade pip -q
    ".venv\Scripts\python.exe" -m pip install -q -r requirements.txt
)
start "" ".venv\Scripts\pythonw.exe" maple_hud.py %*
