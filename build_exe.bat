@echo off
REM Builds the standalone BPSR_MIDI_Player.exe into the dist\ folder.
REM Requires Python 3.8+ with the app dependencies already installed.

py -m pip install pyinstaller
py -m PyInstaller --onefile --windowed --name BPSR_MIDI_Player ^
  --collect-all customtkinter ^
  --hidden-import mido.backends.rtmidi ^
  main.py

echo.
echo Done! The executable is at dist\BPSR_MIDI_Player.exe
pause
