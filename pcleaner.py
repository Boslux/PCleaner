from __future__ import annotations

import argparse
import ctypes
import os
import queue
import sys
import threading
import time
import tkinter as tk
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from tkinter import messagebox, ttk


APP_NAME = "PCleaner"
AGE_CHOICES = (1, 3, 7, 14, 30)
PROGRESS_BATCH = 128
MAX_ISSUES = 200
BG = "#0b1110"
PANEL = "#111a18"
PANEL_2 = "#17221f"
LINE = "#263630"
TEXT = "#e8f1ed"
MUTED = "#8da49a"
GREEN = "#53e0a0"
ORANGE = "#f2b66d"


@dataclass
class Candidate:
    key: str
    root: str
    path: str
    size: int
    modified: float


@dataclass
class ScanIssue:
    path: str
    reason: str


@dataclass
class ScanResult:
    key: str
    label: str
    path: str
    count: int = 0
    size: int = 0
    error: str = ""
    files: list[Candidate] = field(default_factory=list)
    issues: list[ScanIssue] = field(default_factory=list)
    issue_count: int = 0


@dataclass
class CleanResult:
    key: str
    removed: int = 0
    size: int = 0
    skipped: list[ScanIssue] = field(default_factory=list)
    skipped_count: int = 0
    cancelled: bool = False


def add_issue(result: ScanResult, path: str, reason: str) -> None:
    result.issue_count += 1
    if len(result.issues) < MAX_ISSUES:
        result.issues.append(ScanIssue(path, reason))


def _is_linked_dir(path: Path) -> bool:
    try:
        return path.is_symlink() or (hasattr(path, "is_junction") and path.is_junction())
    except OSError:
        return True


def user_temp_root() -> Path:
    return Path(os.environ.get("TEMP", str(Path.home() / "AppData/Local/Temp")))


def windows_temp_root() -> Path:
    windows_dir = os.environ.get("WINDIR") or os.environ.get("SystemRoot") or r"C:\Windows"
    return Path(windows_dir) / "Temp"


def local_app_data() -> Path:
    return Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData/Local")))


def category_roots(key: str) -> list[Path]:
    if key == "user-temp":
        return [user_temp_root()]
    if key == "windows-temp":
        return [windows_temp_root()]
    if key == "recent":
        return [Path(os.environ.get("APPDATA", str(Path.home() / "AppData/Roaming"))) / "Microsoft/Windows/Recent"]
    if key == "browser-cache":
        local = local_app_data()
        roots: list[Path] = []
        for product in ("Google/Chrome/User Data", "Microsoft/Edge/User Data"):
            profiles = local / product
            try:
                roots.extend(profile / "Cache/Cache_Data" for profile in profiles.iterdir() if profile.is_dir() and not _is_linked_dir(profile))
            except OSError:
                pass
        firefox_profiles = local / "Mozilla/Firefox/Profiles"
        try:
            roots.extend(profile / "cache2/entries" for profile in firefox_profiles.iterdir() if profile.is_dir() and not _is_linked_dir(profile))
        except OSError:
            pass
        return roots
    if key == "thumbnail-cache":
        return [local_app_data() / "Microsoft/Windows/Explorer"]
    if key == "recycle":
        return []
    return []


LABELS = {
    "user-temp": "%TEMP% dosyaları",
    "windows-temp": "Windows Temp",
    "recent": "Son kullanılanlar",
    "browser-cache": "Tarayıcı önbellekleri",
    "thumbnail-cache": "Küçük resim önbelleği",
    "recycle": "Geri dönüşüm kutusu",
}
RECURSIVE_CATEGORIES = {"user-temp", "windows-temp", "browser-cache"}
CACHE_CATEGORIES = {"user-temp", "windows-temp", "browser-cache", "thumbnail-cache"}


def matches_category(key: str, filename: str) -> bool:
    if key == "recent":
        return filename.lower().endswith(".lnk")
    if key == "thumbnail-cache":
        lower = filename.lower()
        return (lower.startswith("thumbcache") or lower.startswith("iconcache")) and lower.endswith(".db")
    return True


def scan_category(key: str, days: int, cancel: threading.Event, progress=None) -> ScanResult:
    roots = category_roots(key)
    result = ScanResult(key, LABELS[key], ", ".join(map(str, roots)) or "Windows Geri Dönüşüm Kutusu")
    if key == "recycle":
        info = SHQUERYRECYCLEBIN()
        info.cbSize = ctypes.sizeof(info)
        try:
            code = ctypes.windll.shell32.SHQueryRecycleBinW(None, ctypes.byref(info))
            if code == 0:
                result.count = int(info.i64NumItems)
                result.size = int(info.i64Size)
            else:
                result.error = f"Windows hata kodu: {code}"
        except (AttributeError, OSError) as exc:
            result.error = str(exc)
        return result

    cutoff = time.time() - days * 86400
    visited = 0
    last_progress = time.monotonic()
    for root in roots:
        if cancel.is_set():
            break
        if not root.exists():
            continue
        if key in {"browser-cache", "thumbnail-cache"} and _is_linked_dir(root):
            add_issue(result, str(root), "Güvenlik için bağlantı/junction hedefi taranmadı")
            continue
        stack = [root]
        root_boundary = root.resolve()
        while stack and not cancel.is_set():
            folder = stack.pop()
            try:
                with os.scandir(folder) as entries:
                    for entry in entries:
                        if cancel.is_set():
                            break
                        candidate_path = Path(entry.path)
                        try:
                            if entry.is_dir(follow_symlinks=False):
                                if key in RECURSIVE_CATEGORIES and not _is_linked_dir(candidate_path):
                                    stack.append(candidate_path)
                                continue
                            if not entry.is_file(follow_symlinks=False) or not matches_category(key, entry.name):
                                continue
                            stat = entry.stat(follow_symlinks=False)
                            visited += 1
                            if key in CACHE_CATEGORIES and stat.st_mtime >= cutoff:
                                continue
                            result.files.append(Candidate(key, str(root_boundary), str(candidate_path), stat.st_size, stat.st_mtime))
                            result.count += 1
                            result.size += stat.st_size
                        except OSError as exc:
                            add_issue(result, str(candidate_path), str(exc))
                        if progress and (visited % PROGRESS_BATCH == 0 or time.monotonic() - last_progress > .15):
                            progress(key, visited)
                            last_progress = time.monotonic()
            except OSError as exc:
                add_issue(result, str(folder), str(exc))
    if progress:
        progress(key, visited)
    return result


def scan_categories(keys, days: int, cancel: threading.Event, progress=None) -> dict[str, ScanResult]:
    results = {}
    for key in keys:
        if cancel.is_set():
            break
        results[key] = scan_category(key, days, cancel, progress)
    return results


class SHQUERYRECYCLEBIN(ctypes.Structure):
    _fields_ = [("cbSize", ctypes.c_long), ("i64Size", ctypes.c_longlong), ("i64NumItems", ctypes.c_longlong)]


def format_size(size: int) -> str:
    value = float(size)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return "0 B"


def clean_category(
    key: str,
    scanned: ScanResult,
    excluded: set[tuple[str, str]],
    days: int,
    cancel: threading.Event,
    progress=None,
) -> CleanResult:
    outcome = CleanResult(key)
    if key == "recycle":
        if cancel.is_set():
            outcome.cancelled = True
            return outcome
        try:
            # Confirmation is handled by PCleaner before this Windows API call.
            code = ctypes.windll.shell32.SHEmptyRecycleBinW(None, None, 1)
            if code == 0:
                outcome.removed, outcome.size = scanned.count, scanned.size
            else:
                outcome.skipped_count = 1
                outcome.skipped.append(ScanIssue("Geri Dönüşüm Kutusu", f"Windows hata kodu: {code}"))
        except (AttributeError, OSError) as exc:
            outcome.skipped_count = 1
            outcome.skipped.append(ScanIssue("Geri Dönüşüm Kutusu", str(exc)))
        return outcome

    cutoff = time.time() - days * 86400
    for index, candidate in enumerate(scanned.files, 1):
        if cancel.is_set():
            outcome.cancelled = True
            break
        if (key, candidate.path) in excluded:
            continue
        path = Path(candidate.path)
        try:
            resolved = path.resolve(strict=True)
            boundary = Path(candidate.root).resolve()
            if not resolved.is_relative_to(boundary) or path.is_symlink() or not path.is_file():
                raise OSError("Dosyanın konumu taramadan sonra değişti")
            stat = path.stat()
            if key in CACHE_CATEGORIES and stat.st_mtime >= cutoff:
                raise OSError("Son taramadan sonra değişti; yaş filtresine artık uymuyor")
            path.unlink()
            outcome.removed += 1
            outcome.size += stat.st_size
        except OSError as exc:
            outcome.skipped_count += 1
            if len(outcome.skipped) < MAX_ISSUES:
                outcome.skipped.append(ScanIssue(candidate.path, str(exc)))
        if progress and (index % PROGRESS_BATCH == 0 or index == len(scanned.files)):
            progress(key, index, len(scanned.files))
    return outcome


def cli_main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="pcleaner", description="Hafif Windows temizleme aracı")
    sub = parser.add_subparsers(dest="command")
    scan_parser = sub.add_parser("scan", help="Temizlenebilir alanları tara")
    scan_parser.add_argument("--days", type=int, choices=AGE_CHOICES, default=AGE_CHOICES[0])
    clean_parser = sub.add_parser("clean", help="Seçilen alanları temizle")
    clean_parser.add_argument("areas", nargs="+", choices=[*LABELS, "temp"], help="user-temp, windows-temp, recent, browser-cache, thumbnail-cache, recycle")
    clean_parser.add_argument("--days", type=int, choices=AGE_CHOICES, default=AGE_CHOICES[0])
    clean_parser.add_argument("--yes", action="store_true", help="Terminal onayını atla")
    clean_parser.add_argument("--dry-run", action="store_true", help="Silmeden dosya önizlemesi göster")
    args = parser.parse_args(argv)
    if args.command == "scan":
        cancel = threading.Event()
        print(f"PCleaner · {args.days} günden eski Temp/önbellek dosyaları")
        rows = scan_categories(LABELS, args.days, cancel)
        for key, row in rows.items():
            suffix = f" · {row.error}" if row.error else ""
            print(f"{row.label:<27} {row.count:>6} öğe   {format_size(row.size):>9}{suffix}")
        return 0
    if args.command == "clean":
        keys = list(dict.fromkeys("user-temp" if key == "temp" else key for key in args.areas))
        cancel = threading.Event()
        rows = scan_categories(keys, args.days, cancel)
        print("Seçilen alanlar:")
        for key in keys:
            row = rows[key]
            print(f"  {LABELS[key]}: {row.count} öğe, {format_size(row.size)}")
            for candidate in row.files if args.dry_run else ():
                print(f"    {candidate.path}")
            if row.issue_count:
                print(f"  Tarama sırasında {row.issue_count} konum okunamadı.")
        if args.dry_run:
            print("Önizleme tamamlandı; hiçbir dosya silinmedi.")
            return 0
        if not args.yes and input("Temizlemeyi onaylıyor musun? [e/H] ").strip().lower() not in ("e", "evet", "y", "yes"):
            print("İptal edildi.")
            return 1
        for key in keys:
            result = clean_category(key, rows[key], set(), args.days, cancel)
            print(f"✓ {LABELS[key]}: {result.removed} öğe, {format_size(result.size)} temizlendi" + (f" · {result.skipped_count} öğe atlandı" if result.skipped_count else ""))
            for issue in result.skipped:
                print(f"    ATLANDI: {issue.path} · {issue.reason}")
        return 0
    return launch_gui()


class PCleanerApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("PCleaner · Hafif temizlik")
        self.root.geometry("900x800")
        self.root.minsize(800, 700)
        self.root.configure(bg=BG)
        self.root.option_add("*Font", "{Segoe UI} 10")
        self.rows: dict[str, ScanResult] = {}
        self.selected = {
            "user-temp": tk.BooleanVar(value=True),
            "windows-temp": tk.BooleanVar(value=True),
            "recent": tk.BooleanVar(value=True),
            "browser-cache": tk.BooleanVar(value=False),
            "thumbnail-cache": tk.BooleanVar(value=False),
            "recycle": tk.BooleanVar(value=False),
        }
        self.excluded: set[tuple[str, str]] = set()
        self.days = tk.StringVar(value="1 gün")
        self.events: queue.Queue = queue.Queue()
        self.busy = False
        self.cancel_event: threading.Event | None = None
        self._style()
        self._build()
        self.root.after(100, self._poll_events)
        self.scan()

    def _style(self):
        style = ttk.Style()
        style.theme_use("clam")
        style.configure("TCheckbutton", background=PANEL_2, foreground=TEXT, font=("Segoe UI", 10))
        style.map("TCheckbutton", background=[("active", PANEL_2)], foreground=[("disabled", MUTED)])
        style.configure("Treeview", background=PANEL, fieldbackground=PANEL, foreground=TEXT, rowheight=36, borderwidth=0)
        style.configure("Treeview.Heading", background=PANEL_2, foreground=MUTED, font=("Segoe UI Semibold", 9), relief="flat")
        style.map("Treeview", background=[("selected", "#214437")], foreground=[("selected", TEXT)])

    def _build(self):
        outer = tk.Frame(self.root, bg=BG, padx=32, pady=25)
        outer.pack(fill="both", expand=True)
        top = tk.Frame(outer, bg=BG)
        top.pack(fill="x")
        tk.Label(top, text="PCleaner", bg=BG, fg=TEXT, font=("Segoe UI", 25, "bold")).pack(side="left")
        tk.Label(top, text="●  HAFİF · YEREL · HIZLI", bg=BG, fg=GREEN, font=("Segoe UI", 9, "bold")).pack(side="right", pady=(12, 0))
        tk.Label(outer, text="Gereksiz dosyaları gör, seç ve temizle.", bg=BG, fg=MUTED, font=("Segoe UI", 11)).pack(anchor="w", pady=(2, 20))

        hero = tk.Frame(outer, bg=PANEL, highlightbackground=LINE, highlightthickness=1, padx=20, pady=15)
        hero.pack(fill="x")
        hero_left = tk.Frame(hero, bg=PANEL)
        hero_left.pack(side="left", fill="x", expand=True)
        tk.Label(hero_left, text="TARAMA ÖZETİ", bg=PANEL, fg=MUTED, font=("Segoe UI", 9, "bold")).pack(anchor="w")
        self.total_label = tk.Label(hero_left, text="Taranıyor…", bg=PANEL, fg=TEXT, font=("Segoe UI", 22, "bold"))
        self.total_label.pack(anchor="w", pady=(5, 1))
        toolbar = tk.Frame(hero, bg=PANEL)
        toolbar.pack(side="right", padx=(12, 0))
        tk.Label(toolbar, text="YAŞ FİLTRESİ", bg=PANEL, fg=MUTED, font=("Segoe UI", 8, "bold")).pack(anchor="w")
        self.age_box = ttk.Combobox(toolbar, textvariable=self.days, state="readonly", width=11, values=[f"{day} gün" for day in AGE_CHOICES])
        self.age_box.pack(anchor="e", pady=(5, 4))
        self.age_box.bind("<<ComboboxSelected>>", lambda _event: self.scan())
        buttons = tk.Frame(toolbar, bg=PANEL)
        buttons.pack(anchor="e")
        self.scan_button = tk.Button(buttons, text="⟳  Tara", command=self.scan, bg=PANEL_2, fg=TEXT, activebackground="#22312b", activeforeground=TEXT, relief="flat", padx=12, pady=8, cursor="hand2")
        self.scan_button.pack(side="left")
        self.cancel_button = tk.Button(buttons, text="Durdur", command=self.cancel, bg="#392522", fg="#ffd5c6", activebackground="#56362f", activeforeground=TEXT, relief="flat", padx=12, pady=8, cursor="hand2", state="disabled")
        self.cancel_button.pack(side="left", padx=(8, 0))
        self.progress = ttk.Progressbar(outer, mode="indeterminate", length=200)
        self.progress.pack(fill="x", pady=(8, 2))
        self.status = tk.Label(outer, text="Hazır", bg=BG, fg=MUTED, font=("Segoe UI", 9), anchor="w")
        self.status.pack(fill="x")
        tk.Label(outer, text="Yaş filtresi Temp ve önbellekler içindir; Son Kullanılanlar kısayolları tarihten bağımsız gösterilir.", bg=BG, fg=MUTED, font=("Segoe UI", 8)).pack(anchor="w", pady=(2, 0))

        tk.Label(outer, text="TEMİZLİK ALANLARI  ·  ✓ sütununa tıklayarak seç", bg=BG, fg=MUTED, font=("Segoe UI", 9, "bold")).pack(anchor="w", pady=(16, 8))
        self.table = ttk.Treeview(outer, columns=("area", "selected", "items", "size", "status"), show="headings", selectmode="browse", height=6)
        for column, title, anchor in (("area", "ALAN", "w"), ("selected", "✓", "center"), ("items", "ÖĞE", "e"), ("size", "BOYUT", "e"), ("status", "DURUM", "e")):
            self.table.heading(column, text=title, anchor=anchor)
        self.table.column("area", width=300, anchor="w")
        self.table.column("selected", width=54, anchor="center")
        self.table.column("items", width=90, anchor="e")
        self.table.column("size", width=115, anchor="e")
        self.table.column("status", width=220, anchor="e")
        self.table.pack(fill="x")
        self.table.bind("<Button-1>", self._table_click)
        tk.Label(outer, text="Önizleme: satıra çift tıkla veya dosyaları seçip hariç tut.", bg=BG, fg=MUTED, font=("Segoe UI", 9)).pack(anchor="w", pady=(8, 0))

        bottom = tk.Frame(outer, bg=BG)
        bottom.pack(fill="x", pady=(14, 0))
        self.preview_button = tk.Button(bottom, text="Dosyaları önizle", command=self.open_preview, bg=PANEL_2, fg=TEXT, activebackground="#22312b", activeforeground=TEXT, relief="flat", padx=15, pady=11, cursor="hand2")
        self.preview_button.pack(side="left")
        self.clean_button = tk.Button(bottom, text="✦  Seçilenleri temizle", command=self.clean, bg=GREEN, fg="#092016", activebackground="#79efb8", activeforeground="#092016", relief="flat", padx=19, pady=12, font=("Segoe UI", 10, "bold"), cursor="hand2")
        self.clean_button.pack(side="right")

        footer = tk.Frame(outer, bg=BG)
        footer.pack(fill="x", side="bottom", pady=(16, 0))
        tk.Label(footer, text="Terminal: python pcleaner.py scan --days 7  ·  clean --dry-run ...", bg=BG, fg=MUTED, font=("Consolas", 9)).pack(side="left")
        tk.Label(footer, text="Kilitli dosyalar atlanır", bg=BG, fg=MUTED, font=("Segoe UI", 9)).pack(side="right")

    def _table_click(self, event):
        if self.busy:
            return "break"
        if self.table.identify_region(event.x, event.y) == "cell" and self.table.identify_column(event.x) == "#2":
            key = self.table.identify_row(event.y)
            if key in self.selected:
                self.selected[key].set(not self.selected[key].get())
                self._refresh_table()
                return "break"

    def _refresh_table(self):
        for key in LABELS:
            row = self.rows.get(key, ScanResult(key, LABELS[key], ""))
            state = "●" if self.selected[key].get() else "○"
            status = row.error or (f"{row.issue_count} konum atlandı" if row.issue_count else ("Temizlenebilir" if row.count else "Temiz"))
            values = (LABELS[key], state, row.count, format_size(row.size), status)
            if self.table.exists(key):
                self.table.item(key, values=values)
            else:
                self.table.insert("", "end", iid=key, values=values)

    def _selected_key(self) -> str:
        selection = self.table.selection()
        return selection[0] if selection else "user-temp"

    def _days_value(self) -> int:
        try:
            return int(self.days.get().split()[0])
        except (ValueError, IndexError):
            return AGE_CHOICES[0]

    def _begin(self):
        self.busy = True
        self.cancel_event = threading.Event()
        self.scan_button.configure(state="disabled")
        self.clean_button.configure(state="disabled")
        self.preview_button.configure(state="disabled")
        self.age_box.configure(state="disabled")
        self.table.configure(selectmode="none")
        self.cancel_button.configure(state="normal")
        self.progress.configure(mode="indeterminate")
        self.progress.start(12)

    def _end(self):
        self.busy = False
        self.cancel_button.configure(state="disabled")
        self.scan_button.configure(state="normal")
        self.clean_button.configure(state="normal")
        self.preview_button.configure(state="normal")
        self.age_box.configure(state="readonly")
        self.table.configure(selectmode="browse")
        self.progress.stop()
        self.progress.configure(mode="indeterminate")

    def cancel(self):
        if self.cancel_event and self.busy:
            self.cancel_event.set()
            self.cancel_button.configure(state="disabled")
            self.status.configure(text="İptal isteniyor; sıradaki dosyada duracak…")

    def scan(self):
        if self.busy:
            return
        self._begin()
        self.rows = {}
        self._refresh_table()
        self.total_label.configure(text="Taranıyor…")
        self.status.configure(text=f"{self._days_value()} günlük filtreyle alanlar taranıyor…")
        cancel = self.cancel_event
        days = self._days_value()
        def worker():
            rows = scan_categories(LABELS, days, cancel, lambda key, count: self.events.put(("scan-progress", key, count)))
            self.events.put(("scan-done", rows, cancel.is_set()))
        threading.Thread(target=worker, daemon=True).start()

    def _poll_events(self):
        try:
            while True:
                event = self.events.get_nowait()
                if event[0] == "scan-progress":
                    self.status.configure(text=f"{LABELS[event[1]]}: {event[2]:,} dosya incelendi…")
                elif event[0] == "scan-done":
                    _, rows, cancelled = event
                    if rows and not cancelled:
                        self.rows = rows
                        self._refresh_table()
                        count = sum(row.count for row in rows.values())
                        size = sum(row.size for row in rows.values())
                        self.total_label.configure(text=f"{format_size(size)}  ·  {count} öğe")
                    self._end()
                    self.status.configure(text="Tarama iptal edildi; kısmi sonuçlar temizliğe açılmadı." if cancelled else "Tarama tamamlandı")
                elif event[0] == "clean-progress":
                    _, key, current, total = event
                    if total:
                        self.progress.stop()
                        self.progress.configure(mode="determinate", maximum=total, value=current)
                    self.status.configure(text=f"{LABELS[key]} temizleniyor: {current:,}/{total:,}…")
                elif event[0] == "clean-done":
                    outcomes = event[1]
                    self._end()
                    self._show_report(outcomes)
                    self.status.configure(text="Temizlik bitti; atlanan dosyaların ayrıntıları raporda.")
                    self.scan()
        except queue.Empty:
            pass
        self.root.after(100, self._poll_events)

    def open_preview(self):
        if self.busy:
            return
        if not self.rows:
            messagebox.showinfo(APP_NAME, "Önizleme için önce tarama tamamlanmalı.", parent=self.root)
            return
        window = tk.Toplevel(self.root)
        window.title("PCleaner · Dosya önizlemesi")
        window.geometry("950x620")
        window.configure(bg=BG)
        frame = tk.Frame(window, bg=BG, padx=18, pady=14)
        frame.pack(fill="both", expand=True)
        top = tk.Frame(frame, bg=BG)
        top.pack(fill="x")
        tk.Label(top, text="TEMİZLENECEK DOSYALAR", bg=BG, fg=TEXT, font=("Segoe UI", 15, "bold")).pack(side="left")
        label_to_key = {label: key for key, label in LABELS.items()}
        preview_key = tk.StringVar(value=LABELS[self._selected_key()])
        selector = ttk.Combobox(top, textvariable=preview_key, state="readonly", width=34, values=list(LABELS.values()))
        selector.pack(side="right")
        info = tk.Label(frame, text="", bg=BG, fg=MUTED, anchor="w")
        info.pack(fill="x", pady=(8, 10))
        table = ttk.Treeview(frame, columns=("state", "size", "date", "path"), show="headings", selectmode="extended")
        for col, title in (("state", "DURUM"), ("size", "BOYUT"), ("date", "DEĞİŞTİRİLME"), ("path", "DOSYA YOLU")):
            table.heading(col, text=title, anchor="w")
        table.column("state", width=90, anchor="center")
        table.column("size", width=90, anchor="e")
        table.column("date", width=145, anchor="w")
        table.column("path", width=590, anchor="w")
        table.tag_configure("excluded", foreground="#82918a")
        scroll = ttk.Scrollbar(frame, orient="vertical", command=table.yview)
        xscroll = ttk.Scrollbar(frame, orient="horizontal", command=table.xview)
        table.configure(yscrollcommand=scroll.set, xscrollcommand=xscroll.set)
        table.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        xscroll.pack(side="bottom", fill="x")
        nav = tk.Frame(frame, bg=BG)
        nav.pack(fill="x", pady=(10, 0))
        page = {"index": 0, "ids": {}}
        page_size = 150

        def render_page():
            table.delete(*table.get_children())
            key = label_to_key[preview_key.get()]
            row = self.rows.get(key)
            candidates = row.files if row else []
            start = page["index"] * page_size
            for offset, candidate in enumerate(candidates[start:start + page_size]):
                item_id = str(start + offset)
                page["ids"][item_id] = candidate
                excluded = (candidate.key, candidate.path) in self.excluded
                date = datetime.fromtimestamp(candidate.modified).strftime("%d.%m.%Y %H:%M")
                table.insert("", "end", iid=item_id, values=("Hariç" if excluded else "Dahil", format_size(candidate.size), date, candidate.path), tags=("excluded",) if excluded else ())
            pages = max(1, (len(candidates) + page_size - 1) // page_size)
            page["ids"] = {str(start + offset): candidate for offset, candidate in enumerate(candidates[start:start + page_size])}
            info.configure(text=f"{LABELS[key]} · {len(candidates):,} dosya · {format_size(row.size if row else 0)} · {sum(1 for item in candidates if (key, item.path) in self.excluded)} hariç")
            if key == "recycle":
                info.configure(text=f"Geri Dönüşüm Kutusu: {row.count if row else 0} öğe · {format_size(row.size if row else 0)}. Windows dosya yollarını bu özet API'sinde sunmuyor.")
            pager.configure(text=f"Sayfa {page['index'] + 1}/{pages}")
            previous.configure(state="normal" if page["index"] else "disabled")
            next_button.configure(state="normal" if page["index"] + 1 < pages else "disabled")

        def toggle_selected(include: bool):
            key = label_to_key[preview_key.get()]
            for item_id in table.selection():
                candidate = page["ids"].get(item_id)
                if candidate:
                    pair = (key, candidate.path)
                    self.excluded.discard(pair) if include else self.excluded.add(pair)
            render_page()

        def toggle_row(_event):
            selection = table.selection()
            if selection:
                candidate = page["ids"].get(selection[0])
                if candidate:
                    pair = (label_to_key[preview_key.get()], candidate.path)
                    self.excluded.remove(pair) if pair in self.excluded else self.excluded.add(pair)
                    render_page()

        table.bind("<Double-1>", toggle_row)
        selector.bind("<<ComboboxSelected>>", lambda _event: (page.update(index=0), render_page()))
        previous = tk.Button(nav, text="← Önceki", command=lambda: (page.update(index=max(0, page["index"] - 1)), render_page()), bg=PANEL_2, fg=TEXT, relief="flat", padx=12, pady=8)
        previous.pack(side="left")
        pager = tk.Label(nav, text="", bg=BG, fg=MUTED)
        pager.pack(side="left", padx=12)
        next_button = tk.Button(nav, text="Sonraki →", command=lambda: (page.update(index=page["index"] + 1), render_page()), bg=PANEL_2, fg=TEXT, relief="flat", padx=12, pady=8)
        next_button.pack(side="left")
        tk.Button(nav, text="Seçimi hariç tut", command=lambda: toggle_selected(False), bg=PANEL_2, fg=TEXT, relief="flat", padx=12, pady=8).pack(side="right")
        tk.Button(nav, text="Tekrar dahil et", command=lambda: toggle_selected(True), bg=PANEL_2, fg=TEXT, relief="flat", padx=12, pady=8).pack(side="right", padx=(0, 8))
        render_page()

    def clean(self):
        keys = [key for key, variable in self.selected.items() if variable.get()]
        if not keys:
            messagebox.showinfo(APP_NAME, "Önce temizlemek istediğin alanları seç.", parent=self.root)
            return
        if any(key not in self.rows for key in keys):
            messagebox.showinfo(APP_NAME, "Seçili alanları önce tara.", parent=self.root)
            return
        failed_scans = [f"{LABELS[key]}: {self.rows[key].error}" for key in keys if self.rows[key].error]
        if failed_scans:
            messagebox.showerror(APP_NAME, "Bazı alanlar taranamadı; önce yeniden tara.\n\n" + "\n".join(failed_scans), parent=self.root)
            return
        details = []
        for key in keys:
            row = self.rows[key]
            if key == "recycle":
                count, size = row.count, row.size
            else:
                count = size = 0
                for item in row.files:
                    if (key, item.path) not in self.excluded:
                        count += 1
                        size += item.size
            details.append(f"• {LABELS[key]}: {count} öğe · {format_size(size)}")
        prompt = "Seçilen alanlar temizlensin mi?\n\n" + "\n".join(details) + "\n\nKilitli veya erişilemeyen dosyalar atlanır ve raporlanır."
        if "recycle" in keys:
            prompt += "\nGeri Dönüşüm Kutusu kalıcı olarak boşaltılır; işlem başladıktan sonra durdurulamaz."
        if not messagebox.askyesno(APP_NAME, prompt, parent=self.root):
            return
        self._begin()
        cancel = self.cancel_event
        days = self._days_value()
        excluded = set(self.excluded)
        order = [key for key in keys if key != "recycle"] + (["recycle"] if "recycle" in keys else [])
        def worker():
            outcomes = {}
            for key in order:
                if cancel.is_set():
                    outcomes[key] = CleanResult(key, cancelled=True)
                else:
                    outcomes[key] = clean_category(key, self.rows[key], excluded, days, cancel, lambda k, current, total: self.events.put(("clean-progress", k, current, total)))
            self.events.put(("clean-done", outcomes))
        threading.Thread(target=worker, daemon=True).start()

    def _show_report(self, outcomes: dict[str, CleanResult]):
        window = tk.Toplevel(self.root)
        window.title("PCleaner · Temizlik raporu")
        window.geometry("820x540")
        window.configure(bg=BG)
        frame = tk.Frame(window, bg=BG, padx=18, pady=16)
        frame.pack(fill="both", expand=True)
        removed = sum(item.removed for item in outcomes.values())
        size = sum(item.size for item in outcomes.values())
        tk.Label(frame, text=f"{removed:,} öğe temizlendi · {format_size(size)}", bg=BG, fg=GREEN, font=("Segoe UI", 17, "bold")).pack(anchor="w")
        text = tk.Text(frame, bg=PANEL, fg=TEXT, insertbackground=TEXT, relief="flat", wrap="none", font=("Consolas", 9))
        scroll = ttk.Scrollbar(frame, orient="vertical", command=text.yview)
        text.configure(yscrollcommand=scroll.set)
        text.pack(side="left", fill="both", expand=True, pady=(14, 0))
        scroll.pack(side="right", fill="y", pady=(14, 0))
        for key, outcome in outcomes.items():
            text.insert("end", f"{LABELS[key]}: {outcome.removed} temizlendi · {format_size(outcome.size)}\n")
            if outcome.cancelled:
                text.insert("end", "  İşlem kullanıcı tarafından durduruldu.\n")
            if outcome.skipped_count:
                text.insert("end", f"  {outcome.skipped_count} öğe atlandı:\n")
                for issue in outcome.skipped:
                    text.insert("end", f"    {issue.path}\n      {issue.reason}\n")
                if outcome.skipped_count > len(outcome.skipped):
                    text.insert("end", f"    … {outcome.skipped_count - len(outcome.skipped)} ek öğe\n")
            row = self.rows.get(key)
            if row and row.issue_count:
                text.insert("end", f"  Tarama sırasında {row.issue_count} konuma erişilemedi:\n")
                for issue in row.issues:
                    text.insert("end", f"    {issue.path}\n      {issue.reason}\n")
            text.insert("end", "\n")
        text.configure(state="disabled")

def launch_gui() -> int:
    if os.name != "nt":
        print("PCleaner'ın masaüstü temizleme özellikleri Windows içindir.", file=sys.stderr)
        return 2
    try:
        root = tk.Tk()
    except tk.TclError as exc:
        print(f"Arayüz başlatılamadı: {exc}", file=sys.stderr)
        return 2
    PCleanerApp(root)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(cli_main(sys.argv[1:]))
