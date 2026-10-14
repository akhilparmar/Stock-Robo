' Starts the paper server without a console window (for Windows Startup folder).
' Trading still runs on this PC — open the Nifty page on your phone to monitor.
Set sh = CreateObject("Wscript.Shell")
sh.CurrentDirectory = CreateObject("Scripting.FileSystemObject").GetParentFolderName(WScript.ScriptFullName) & "\.."
sh.Run """" & sh.CurrentDirectory & "\.venv\Scripts\uvicorn.exe"" app.main:app --host 0.0.0.0 --port 8010", 0, False
