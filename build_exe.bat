@echo off
python -m pip install -r requirements.txt pyinstaller
python -m PyInstaller --onefile --noconsole --name UwUMicClient uwumic_client.py
echo.
echo Done. Your program is dist\UwUMicClient.exe
pause
