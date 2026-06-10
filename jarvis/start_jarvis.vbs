' ─────────────────────────────────────────────
'  J.A.R.V.I.S. — Silent Launcher
'  Double-click this file to start JARVIS with
'  NO visible command-prompt / console window.
'
'  Made by Vanitas
' ─────────────────────────────────────────────

Option Explicit

Dim objShell, strScript, strDir

Set objShell = CreateObject("WScript.Shell")

' Get the folder this .vbs lives in
strDir = Left(WScript.ScriptFullName, InStrRev(WScript.ScriptFullName, "\"))

' Path to jarvis_gui.py (same folder as this script)
strScript = strDir & "jarvis_gui.py"

' Run pythonw (no console) — 0 = hidden window, False = don't wait
' pythonw.exe is the windowless variant that ships with every Python install
objShell.Run "pythonw """ & strScript & """", 0, False

Set objShell = Nothing
