@echo off
rem Igual que la primera version: token, sin iniciar sesion en Witlink.
rem SGA debe quedar abierto. El token va en token.txt.
cd /d "%~dp0"
python -m pip install -q -r requirements.txt
python bot_paquetes.py --auto
pause
