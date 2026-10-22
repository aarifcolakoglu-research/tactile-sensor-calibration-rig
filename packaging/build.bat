@echo off
cd /d "%~dp0.."
pyinstaller --noconfirm --clean packaging\ATI_Rig.spec
pause
