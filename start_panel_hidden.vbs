Set shell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
folder = fso.GetParentFolderName(WScript.ScriptFullName)
shell.CurrentDirectory = folder
shell.Run "cmd /c if not exist work mkdir work && python app.py > work\panel_server.log 2>&1", 0, False
WScript.Sleep 3000
shell.Run "http://127.0.0.1:5000", 1, False
