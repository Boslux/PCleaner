import heapq
import json
import os
import stat
import subprocess
from pathlib import Path


def startup_entries():
    import winreg

    version = r"Software\Microsoft\Windows\CurrentVersion"
    approved = version + r"\Explorer\StartupApproved"
    user, machine = winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE
    entries = []
    unavailable = 0

    def add(hive, name, source, bucket, scope):
        try:
            with winreg.OpenKey(hive, approved + "\\" + bucket, 0, winreg.KEY_READ | winreg.KEY_WOW64_64KEY) as key:
                value, _ = winreg.QueryValueEx(key, name)
        except FileNotFoundError:
            value = b"\x02"
        except OSError:
            value = b""
        # shortcut: StartupApproved covers classic entries; use Windows for the full startup inventory.
        code = value[0] if isinstance(value, bytes) and value else None
        status = {2: "Etkin", 3: "Devre dışı", 6: "Etkin", 7: "Devre dışı"}.get(code, "Bilinmiyor")
        entries.append((name, status, scope, source))

    for hive, scope, path, bucket in (
        (user, "Bu kullanıcı", version + r"\Run", "Run"),
        (machine, "Tüm kullanıcılar", version + r"\Run", "Run"),
        (machine, "Tüm kullanıcılar", r"Software\Wow6432Node\Microsoft\Windows\CurrentVersion\Run", "Run32"),
    ):
        try:
            with winreg.OpenKey(hive, path, 0, winreg.KEY_READ | winreg.KEY_WOW64_64KEY) as key:
                for index in range(winreg.QueryInfoKey(key)[1]):
                    name, command, _ = winreg.EnumValue(key, index)
                    add(hive, name, str(command), bucket, scope)
        except FileNotFoundError:
            continue
        except OSError:
            unavailable += 1

    for hive, scope, variable in ((user, "Bu kullanıcı", "APPDATA"), (machine, "Tüm kullanıcılar", "ProgramData")):
        base = os.environ.get(variable)
        if base:
            folder = Path(base) / "Microsoft/Windows/Start Menu/Programs/Startup"
            try:
                for path in folder.iterdir():
                    if path.is_file() and path.name.lower() != "desktop.ini":
                        add(hive, path.name, str(path), "StartupFolder", scope)
            except FileNotFoundError:
                continue
            except OSError:
                unavailable += 1
    return sorted(entries, key=lambda entry: entry[0].casefold()), unavailable


def resource_snapshot():
    command = """
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$processes = @(Get-CimInstance Win32_PerfFormattedData_PerfProc_Process |
    Where-Object { $_.IDProcess -gt 0 } |
    Select-Object Name, IDProcess, PercentProcessorTime, WorkingSetPrivate, IODataBytesPersec)
$disk = $null
try {
    $disk = (Get-CimInstance Win32_PerfFormattedData_PerfDisk_PhysicalDisk -Filter "Name='_Total'").DiskBytesPersec
} catch {}
@{ Processes = $processes; Disk = $disk } | ConvertTo-Json -Compress
"""
    powershell = Path(os.environ["SystemRoot"]) / "System32/WindowsPowerShell/v1.0/powershell.exe"
    result = subprocess.run(
        [str(powershell), "-NoProfile", "-NonInteractive", "-Command", command],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=20,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    if result.returncode:
        raise OSError("Windows performans sayaçları okunamadı.")
    data = json.loads(result.stdout)
    cores = os.cpu_count() or 1
    return [
        (item["Name"], item["IDProcess"], (item["PercentProcessorTime"] or 0) / cores,
         item["WorkingSetPrivate"] or 0, item["IODataBytesPersec"] or 0)
        for item in data["Processes"]
    ], data["Disk"]


def largest_files(root, cancel, limit=50):
    if cancel.is_set():
        return [], 0
    pending = [root]
    largest = []
    skipped = 0
    while pending and not cancel.is_set():
        folder = pending.pop()
        try:
            with os.scandir(folder) as entries:
                for entry in entries:
                    if cancel.is_set():
                        break
                    try:
                        info = entry.stat(follow_symlinks=False)
                    except OSError:
                        skipped += 1
                        continue
                    if info.st_file_attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT:
                        skipped += 1
                    elif stat.S_ISDIR(info.st_mode):
                        pending.append(entry.path)
                    elif stat.S_ISREG(info.st_mode):
                        item = (info.st_size, entry.path)
                        if len(largest) < limit:
                            heapq.heappush(largest, item)
                        else:
                            heapq.heappushpop(largest, item)
        except OSError:
            skipped += 1
    return sorted(largest, reverse=True), skipped
