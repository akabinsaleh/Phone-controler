' Starts the PC Remote supervisor hidden (no console window), which runs and
' restarts the agent. Safe to run repeatedly: a second supervisor exits at once.
' Run by the "PCRemote Agent" task (logon) and "PCRemote Watchdog" task (every 5 min).
Set sh = CreateObject("WScript.Shell")
sh.CurrentDirectory = "C:\Users\zzazi\PCRemote"
sh.Run """C:\Users\zzazi\AppData\Local\Programs\Python\Python314\pythonw.exe"" ""C:\Users\zzazi\PCRemote\supervisor.py""", 0, False
