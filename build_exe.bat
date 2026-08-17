@echo off
REM ============================================================
REM  Compila pump_driver.py a un .exe independiente (--onefile)
REM  Requiere Python 3.9+ instalado y en el PATH.
REM ============================================================
chcp 65001 >nul
cd /d "%~dp0"

echo [1/3] Instalando dependencias...
pip install -r requirements.txt || goto :error

echo [2/3] Compilando con PyInstaller...
pyinstaller --noconfirm --onefile --windowed --name PumpFSRDriver --collect-all customtkinter pump_driver.py || goto :error

echo [3/3] Listo.
echo.
echo El ejecutable se genero en: dist\PumpFSRDriver.exe
echo (la configuracion de umbrales se guarda junto al .exe)
goto :eof

:error
echo.
echo ERROR: el paso anterior fallo. Revisa los mensajes de arriba.
exit /b 1
