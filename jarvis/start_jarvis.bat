@echo off
:: ─────────────────────────────────────────────
::  J.A.R.V.I.S. — Batch Launcher (hidden console)
::  Uses start /B with pythonw to suppress the window.
::  Made by Vanitas
:: ─────────────────────────────────────────────

:: Change to the script's own directory
cd /d "%~dp0"

:: Use pythonw.exe — the windowless Python interpreter
:: /B runs in the same window (no new cmd box), pythonw hides it entirely
start "" /B pythonw "%~dp0jarvis_gui.py"
