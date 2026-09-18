#!/usr/bin/env bash
# ============================================================
#  Compila pump_driver.py a un binario independiente (--onefile)
#  para Linux. Requiere Python 3.9+ y pip.
#  (el equivalente a build_exe.bat en Windows)
# ============================================================
set -euo pipefail
cd "$(dirname "$0")"

echo "[1/3] Instalando dependencias..."
pip install -r requirements.txt

echo "[2/3] Compilando con PyInstaller..."
python -m PyInstaller --noconfirm --onefile --windowed --name PumpFSRDriver --collect-all customtkinter pump_driver.py

echo "[3/3] Listo."
echo
echo "El ejecutable se genero en: dist/PumpFSRDriver"
echo "(la configuracion de umbrales se guarda junto al binario)"