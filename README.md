# PCleaner

**Lightweight, fast, and straightforward disk cleanup for Windows.** Preview temporary files and selected caches before cleanup. Manage the process from a simple desktop interface or directly from your terminal, while files in use are skipped.

PCleaner runs locally and has no runtime dependencies beyond the Python standard library and Tkinter.

## Features

- Scans both the current user's `%TEMP%` folder and the Windows `Temp` folder.
- Previews scan results before cleanup and lets you exclude individual files.
- Skips files that are locked by another application or cannot be accessed, continues with the remaining files, and reports skipped items.
- Removes shortcuts from the Recent Items folder without deleting their target files.
- Offers optional browser cache and Windows thumbnail cache cleanup.
- Supports terminal scanning, dry runs, age filters, reports, and cancellation.
- Shows selected cleanup areas and the amount of space recovered.
- Scans selected cleanup categories in the desktop app and reports elapsed scan time.
- Lists classic Windows startup entries and opens Task Manager to manage them.
- Shows a sortable CPU, private RAM and process I/O snapshot, plus total physical disk throughput.
- Finds the 50 largest files on a selected local drive and opens their folders in Explorer.

## Quick start

Open PowerShell or Windows Terminal in the project folder and run:

```powershell
python pcleaner.py
```

You can also double-click `PCleaner.bat` on Windows.

## Use from the terminal

Scan eligible files older than seven days:

```powershell
python pcleaner.py scan --days 7
```

Preview a cleanup without deleting anything:

```powershell
python pcleaner.py clean user-temp windows-temp browser-cache --days 7 --dry-run
```

Run a cleanup with an interactive confirmation:

```powershell
python pcleaner.py clean user-temp windows-temp recent
```

The `clean` command asks for confirmation unless you pass `--yes`. `--dry-run` does not delete files. Supported age filters are 1, 3, 7, 14, and 30 days.

### Cleanup areas

| Command | What it cleans |
| --- | --- |
| `user-temp` | Eligible files in the current user's `%TEMP%` folder |
| `windows-temp` | Eligible files in the `Temp` folder under the Windows directory |
| `recent` | `.lnk` shortcuts in Recent Items; target files are preserved |
| `browser-cache` | Cache files in Chrome, Edge, and Firefox profiles |
| `thumbnail-cache` | Windows Explorer thumbnail and icon cache databases |
| `recycle` | The Recycle Bin; items are permanently deleted |

`temp` is a legacy alias for `user-temp`. Browser history, cookies, and session files are not included in browser cache cleanup. Browser and thumbnail caches are not selected by default. Locked or inaccessible items are skipped while the remaining files are processed.

## Build the portable EXE

PyInstaller is required to build the EXE. The built application does not require Python to be installed on the target computer.

```powershell
python -m pip install -r requirements-build.txt
.\build_exe.ps1
```

The outputs are `release\PCleaner\PCleaner.exe` and `release\PCleaner-portable.zip`. Keep the `_internal` folder next to the EXE, or use the ZIP, which contains the complete folder.

## Safe use

- Review the file preview in the app or use `--dry-run` before your first cleanup.
- Cleanup only processes the areas you select; the app does not change system settings.
- Recycle Bin cleanup permanently deletes its contents.
