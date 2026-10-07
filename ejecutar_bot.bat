@echo off
rem Ejecuta el bot de paquetes desde esta carpeta (doble clic).
rem La primera vez instala lo que necesita (playwright y pywinauto).
cd /d "%~dp0"
python -m pip install -q -r requirements.txt
python bot_paquetes.py
pause
