Option Explicit

Dim shell, fileSystem, projectDir, launcher, installer
Set shell = CreateObject("WScript.Shell")
Set fileSystem = CreateObject("Scripting.FileSystemObject")

projectDir = fileSystem.GetParentFolderName(WScript.ScriptFullName)
launcher = fileSystem.BuildPath(projectDir, ".venv\Scripts\school-notice-assistant.exe")
installer = fileSystem.BuildPath(projectDir, "install.bat")

If Not fileSystem.FileExists(launcher) Then
    MsgBox "Runtime not found:" & vbCrLf & launcher & vbCrLf & vbCrLf & _
           "First-time setup: double-click install.bat, then run this vbs again.", _
           vbCritical, "School Notice Assistant"
    WScript.Quit 1
End If

shell.CurrentDirectory = projectDir
shell.Run Chr(34) & launcher & Chr(34), 0, False