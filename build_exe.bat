@echo off
python -m pip install -r requirements.txt pyinstaller
set EXTRA=
if exist vbcable.zip set EXTRA=--add-data "vbcable.zip;."
python -m PyInstaller --noconfirm --onedir --noconsole --name UwUMicClient --icon uwumic.ico --version-file version_info.txt --add-data "uwumic.ico;." --collect-all pyrnnoise --exclude-module audiolab --exclude-module av %EXTRA% uwumic_client.py
echo.
echo Done. Your program is the folder dist\UwUMicClient (run UwUMicClient.exe inside it)
pause
