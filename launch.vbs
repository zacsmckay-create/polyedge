Set WShell = CreateObject("WScript.Shell")
Dim folder
folder = CreateObject("Scripting.FileSystemObject").GetParentFolderName(WScript.ScriptFullName)
WShell.Run "cmd /c """ & folder & "\Start-PolyEdge.bat""", 0, False
