@echo off
python -m pip install -r requirements.txt pyinstaller
set EXTRA=
if exist vbcable.zip set EXTRA=--add-data "vbcable.zip;."
python -m PyInstaller --onefile --noconsole --name UwUMicClient --icon uwumic.ico --add-data "uwumic.ico;." %EXTRA% uwumic_client.py
echo.
echo Done. Your program is dist\UwUMicClient.exe
pause
