@echo off
REM Build a lab-distributable of data_acquisition.
REM   build_exe.bat              -> dist\DataAcquisition\  + a ready-to-hand-out .zip
REM   build_exe.bat --onefile    -> single DataAcquisition.exe (+ .zip)
REM Needs:  pip install -r requirements.txt
cd /d "%~dp0"
python build.py %*
echo.
pause
