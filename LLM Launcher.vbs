Set WshShell = CreateObject("WScript.Shell")
Set envProc = WshShell.Environment("Process")
envProc.Remove("PYTHONHOME")
envProc.Remove("PYTHONPATH")
envProc.Item("PYTHONHOME") = ""
envProc.Item("PYTHONPATH") = ""
WshShell.Run "cmd /c set ""PYTHONHOME="" & set ""PYTHONPATH="" & ""C:\Users\Cyber\AppData\Local\Programs\Python\Python312\pythonw.exe"" ""E:\Ai\llama.cpp\launcher\launcher.py""", 0, False
