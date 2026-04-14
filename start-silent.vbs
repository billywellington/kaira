Set WshShell = CreateObject("WScript.Shell")
Dim scriptDir
scriptDir = Replace(WScript.ScriptFullName, "\start-silent.vbs", "")
WshShell.Run """" & scriptDir & "\.venv312\Scripts\pythonw.exe"" """ & scriptDir & "\voice_dictation.py""", 0, False
