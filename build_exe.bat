@echo off
rem Local build of the installed layout (what CI does): dist\MapleFarmTracker\MapleFarmTracker.exe
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" call run.bat --version
".venv\Scripts\python.exe" -m pip install -q pyinstaller
".venv\Scripts\python.exe" -m PyInstaller --noconfirm --clean MapleFarmTracker.spec
echo Built dist\MapleFarmTracker\  (installer needs Inno Setup 6: ISCC /DMyAppVersion=0.3.0 installer\installer.iss)
pause
