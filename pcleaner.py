from __future__ import annotations

import argparse
import ctypes
import os
import queue
import shutil
import subprocess
import sys
import threading
import time
import tkinter as tk
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from tkinter import messagebox, ttk

from windows_performance import largest_files, resource_snapshot, startup_entries


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
AREA_HELP = {
    "user-temp": "%TEMP%, uygulamaların geçici çalışma dosyalarını tuttuğu kullanıcı klasörüdür.",
    "windows-temp": "Windows ve kurulum programlarının geçici sistem dosyalarıdır. Bazı dosyalar yönetici izni gerektirebilir.",
    "recent": "Windows’un son açılan dosyalar için oluşturduğu kısayollardır. Asıl dosyalar korunur.",
    "browser-cache": "Chrome, Edge ve Firefox’un sayfaları hızlandırmak için sakladığı geçici dosyalardır. Çerezler ve geçmiş korunur.",
    "thumbnail-cache": "Dosya Gezgini’nin küçük resim ve simge önbelleğidir. Windows gerektiğinde yeniden oluşturur.",
    "recycle": "Geri dönüşüm kutusundaki öğeleri kalıcı olarak siler. Silmeden önce içeriğini Windows’ta kontrol edebilirsin.",
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
        self.root.geometry("940x720")
        self.root.minsize(820, 680)
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
        self.system_drive = windows_temp_root().anchor or "C:\\"
        self._build()
        self._refresh_disk(self._disk_usage())
        self.root.after(100, self._poll_events)
        self.scan()

    def _style(self):
        style = ttk.Style()
        style.theme_use("clam")
        style.configure("TNotebook", background=BG, borderwidth=0)
        style.configure("TNotebook.Tab", background=PANEL_2, foreground=MUTED,
                        padding=(18, 10), font=("Segoe UI", 11, "bold"))
        style.map("TNotebook.Tab", background=[("selected", GREEN)], foreground=[("selected", BG)])
        style.configure("Area.TCheckbutton", background=PANEL_2, foreground=TEXT,
                        font=("Segoe UI", 10, "bold"), padding=(10, 7),
                        indicatorsize=19, indicatorbackground=PANEL)
        style.map("Area.TCheckbutton", background=[("selected", "#214437"), ("active", LINE)],
                  indicatorbackground=[("selected", GREEN)],
                  indicatorforeground=[("selected", BG)], foreground=[("disabled", MUTED)])
        style.configure("Treeview", background=PANEL, fieldbackground=PANEL, foreground=TEXT, rowheight=36, borderwidth=0)
        style.configure("Treeview.Heading", background=PANEL_2, foreground=MUTED, font=("Segoe UI Semibold", 9), relief="flat")
        style.map("Treeview", background=[("selected", "#214437")], foreground=[("selected", TEXT)])

    def _build(self):
        outer = tk.Frame(self.root, bg=BG, padx=32, pady=25)
        outer.pack(fill="both", expand=True)
        top = tk.Frame(outer, bg=BG)
        top.pack(fill="x")
        tk.Label(top, text="PCleaner", bg=BG, fg=TEXT, font=("Segoe UI", 25, "bold")).pack(side="left")
        self.pages = ttk.Notebook(outer)
        self.pages.pack(fill="both", expand=True, pady=(16, 8))
        cleanup = tk.Frame(self.pages, bg=BG, pady=12)
        self.disk_page = tk.Frame(self.pages, bg=BG, pady=16)
        report_page = tk.Frame(self.pages, bg=BG, pady=16)
        self.pages.add(cleanup, text="Temizlik")
        self.pages.add(self.disk_page, text="Disk Alanı")
        self.pages.add(report_page, text="Sonuçlar")

        hero = tk.Frame(cleanup, bg=PANEL, highlightbackground=LINE, highlightthickness=1, padx=20, pady=15)
        hero.pack(fill="x")
        hero_left = tk.Frame(hero, bg=PANEL)
        hero_left.pack(side="left", fill="x", expand=True)
        tk.Label(hero_left, text="SON TARAMA", bg=PANEL, fg=MUTED, font=("Segoe UI", 9, "bold")).pack(anchor="w")
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
        self.scan_button = tk.Button(buttons, text="Seçilenleri tara", command=self.scan, bg=PANEL_2, fg=TEXT, activebackground="#22312b", activeforeground=TEXT, relief="flat", padx=12, pady=8, cursor="hand2")
        self.scan_button.pack(side="left")
        self.cancel_button = tk.Button(buttons, text="Durdur", command=self.cancel, bg="#392522", fg="#ffd5c6", activebackground="#56362f", activeforeground=TEXT, relief="flat", padx=12, pady=8, cursor="hand2", state="disabled")
        self.cancel_button.pack(side="left", padx=(8, 0))
        self.progress = ttk.Progressbar(outer, mode="indeterminate", length=200)
        self.progress.pack(fill="x", pady=(8, 2))
        self.status = tk.Label(outer, text="Hazır", bg=BG, fg=MUTED, font=("Segoe UI", 9), anchor="w")
        self.status.pack(fill="x")
        help_text = "Yaş filtresi geçici dosyalara ve önbelleklere uygulanır."
        tk.Label(cleanup, text=help_text, bg=BG, fg=MUTED).pack(anchor="w", pady=(8, 4))
        self.area_controls = {}
        self.area_results = {}
        for key, label in LABELS.items():
            row = tk.Frame(cleanup, bg=PANEL_2, padx=8, pady=3)
            row.pack(fill="x", pady=2)
            check = ttk.Checkbutton(row, text=label, variable=self.selected[key], style="Area.TCheckbutton", width=27)
            check.pack(side="left")
            ttk.Button(row, text="ⓘ  Nedir?", command=lambda k=key: self._show_area_help(k)).pack(side="right")
            preview = ttk.Button(row, text="Dosyaları gör", command=lambda k=key: self.open_preview(k))
            preview.pack(side="right", padx=8)
            self.area_controls[key] = (check, preview)
            result = tk.Label(row, bg=PANEL_2, fg=TEXT, anchor="w")
            result.pack(side="left", fill="x", expand=True, padx=8)
            self.area_results[key] = result

        self.clean_button = tk.Button(
            cleanup, text="Seçilenleri temizle", command=self.clean, bg=GREEN, fg=BG,
            activebackground="#8af1c2", activeforeground=BG, font=("Segoe UI", 11, "bold"),
            relief="flat", padx=18, pady=9, cursor="hand2",
        )
        self.clean_button.pack(anchor="e", pady=(12, 0))
        self._build_disk_page()
        self._build_performance_pages()
        self.report_title = tk.Label(report_page, text="Henüz temizlik yapılmadı", bg=BG, fg=TEXT,
                                     font=("Segoe UI", 17, "bold"))
        self.report_title.pack(anchor="w")
        self.report_text = tk.Text(report_page, bg=PANEL, fg=TEXT, relief="flat", wrap="word", state="disabled")
        scroll = ttk.Scrollbar(report_page, orient="vertical", command=self.report_text.yview)
        self.report_text.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y", pady=(14, 0))
        self.report_text.pack(side="left", fill="both", expand=True, pady=(14, 0))

    def _performance_page(self, name, columns):
        page = tk.Frame(self.pages, bg=BG, pady=12)
        self.pages.add(page, text=name)
        toolbar = tk.Frame(page, bg=BG)
        toolbar.pack(fill="x", pady=(0, 8))
        note = tk.Label(page, bg=BG, fg=MUTED, anchor="w", wraplength=700, justify="left")
        note.pack(fill="x", pady=(0, 8))
        table = ttk.Treeview(page, columns=tuple(range(len(columns))), show="headings", selectmode="browse")
        for index, (label, width) in enumerate(columns):
            table.heading(str(index), text=label)
            table.column(str(index), width=width, anchor="w", stretch=index == len(columns) - 1)
        yscroll = ttk.Scrollbar(page, orient="vertical", command=table.yview)
        xscroll = ttk.Scrollbar(page, orient="horizontal", command=table.xview)
        table.configure(yscrollcommand=yscroll.set, xscrollcommand=xscroll.set)
        xscroll.pack(side="bottom", fill="x")
        yscroll.pack(side="right", fill="y")
        table.pack(fill="both", expand=True)
        return page, toolbar, note, table

    def _build_performance_pages(self):
        self.performance_buttons = []
        _, bar, startup_note, table_startup = self._performance_page("Başlangıç", (
            ("Uygulama", 160), ("Durum", 100), ("Kapsam", 125), ("Komut veya konum", 315),
        ))
        startup_hint = "Run kayıtları ve Başlangıç klasörleri. Değişiklikler için Görev Yöneticisi’nde Başlangıç uygulamaları’nı aç."
        startup_note.configure(text=startup_hint)

        def show_startup(result):
            rows, unavailable = result
            summary = f"{len(rows)} kayıt · {unavailable} kaynak okunamadı." if unavailable else f"{len(rows)} kayıt."
            startup_note.configure(text=f"{summary} {startup_hint}")
            self._fill_performance(table_startup, rows)

        self._performance_button(bar, "Listeyi yenile", lambda: self._performance_job(
            "Başlangıç uygulamaları okunuyor…", lambda _: startup_entries(),
            show_startup,
        ))
        ttk.Button(bar, text="Görev Yöneticisi’ni aç",
                   command=lambda: self._open_location(str(windows_temp_root().parent / "System32/Taskmgr.exe"))).pack(side="left", padx=8)

        _, bar, self.resource_note, self.process_table = self._performance_page("Kaynak Kullanımı", (
            ("İşlem", 190), ("PID", 75), ("CPU", 90), ("Özel RAM", 115), ("İşlem G/Ç", 160),
        ))
        self.resource_note.configure(text="Anlık ölçüm al. İşlem G/Ç sayacı ağ ve diğer giriş/çıkış işlemlerini de kapsar.")
        self.process_rows = []
        order = ttk.Combobox(bar, values=("CPU", "RAM", "G/Ç"), state="readonly", width=8)
        order.current(0)
        order.pack(side="left", padx=(0, 8))
        order.bind("<<ComboboxSelected>>", lambda _: self._render_processes(order.current()))
        self._performance_button(bar, "Ölçümü yenile", lambda: self._performance_job(
            "Kaynak kullanımı ölçülüyor…", lambda _: resource_snapshot(),
            lambda result: self._show_resources(result, order.current()),
        ))
        ttk.Button(bar, text="Kaynak İzleyicisi’ni aç",
                   command=lambda: self._open_location(str(windows_temp_root().parent / "System32/resmon.exe"))).pack(side="left", padx=8)

        _, bar, note, table = self._performance_page("Büyük Dosyalar", (
            ("Boyut", 110), ("Dosya yolu", 580),
        ))
        note.configure(text="Bir sürücü seç. Boyuta göre ilk 50 dosya gösterilir.")
        mask = ctypes.windll.kernel32.GetLogicalDrives()
        drives = [f"{chr(65 + index)}:\\" for index in range(26) if mask & (1 << index)]
        drives = [path for path in drives if ctypes.windll.kernel32.GetDriveTypeW(path) in (2, 3)]
        drive = ttk.Combobox(bar, values=drives, state="readonly", width=8)
        drive.set(self.system_drive)
        drive.pack(side="left", padx=(0, 8))

        def scan_drive():
            selected = drive.get()
            if selected not in drives:
                note.configure(text="Taramak için listeden bir sürücü seç.")
                return
            note.configure(text=f"Taranan sürücü: {selected}")
            self._fill_performance(table, [])
            self._performance_job(f"{selected} sürücüsü taranıyor…",
                                  lambda cancel: largest_files(selected, cancel),
                                  lambda result: show_files(selected, result), cancellable=True)

        def show_files(selected, result):
            rows, skipped = result
            note.configure(text=f"{selected} · {len(rows)} dosya · {skipped:,} konum atlandı")
            self._fill_performance(table, [(format_size(size), path) for size, path in rows])

        def open_file_folder():
            selection = table.selection()
            if selection:
                path = table.item(selection[0], "values")[1]
                folder = os.path.dirname(path)
                if os.path.isdir(folder):
                    self._open_location(folder)
                else:
                    messagebox.showinfo(APP_NAME, "Dosyanın klasörüne artık erişilemiyor.", parent=self.root)

        self._performance_button(bar, "Büyük dosyaları bul", scan_drive)
        ttk.Button(bar, text="Dosyanın klasörünü aç", command=open_file_folder).pack(side="left", padx=8)

    def _performance_button(self, parent, label, command):
        button = ttk.Button(parent, text=label, command=command)
        button.pack(side="left")
        self.performance_buttons.append(button)

    def _performance_job(self, status, work, render, cancellable=False):
        self._begin()
        self.status.configure(text=status)
        if not cancellable:
            self.cancel_button.configure(state="disabled")
        cancel = self.cancel_event

        def worker():
            try:
                self.events.put(("performance-done", render, work(cancel), None))
            except (OSError, ValueError, TimeoutError, subprocess.TimeoutExpired) as exc:
                self.events.put(("performance-done", render, None, str(exc)))

        threading.Thread(target=worker, daemon=True).start()

    def _fill_performance(self, table, rows):
        table.delete(*table.get_children())
        for row in rows:
            table.insert("", "end", values=row)

    def _show_resources(self, snapshot, order):
        self.process_rows, disk = snapshot
        activity = f"{format_size(disk)}/sn" if disk is not None else "ölçülemedi"
        self.resource_note.configure(text=f"Toplam fiziksel disk aktarımı: {activity}. İşlem G/Ç sayacı ağ ve diğer giriş/çıkış işlemlerini de kapsar. İşlem bazında disk için Kaynak İzleyicisi’nde Disk sekmesini aç.")
        self._render_processes(order)

    def _render_processes(self, order):
        rows = sorted(self.process_rows, key=lambda row: row[2 + order], reverse=True)
        self._fill_performance(self.process_table, [
            (name, pid, f"%{cpu:.1f}", format_size(ram), f"{format_size(io)}/sn")
            for name, pid, cpu, ram, io in rows
        ])

    def _open_location(self, path):
        try:
            os.startfile(path)
        except OSError as exc:
            messagebox.showerror(APP_NAME, str(exc), parent=self.root)

    def _show_area_help(self, key: str):
        messagebox.showinfo(LABELS[key], AREA_HELP[key], parent=self.root)

    def _refresh_areas(self):
        for key, label in self.area_results.items():
            row = self.rows.get(key)
            if row is None:
                label.configure(text="Taranmadı", fg=MUTED)
            elif row.error:
                label.configure(text="Taranamadı", fg=ORANGE)
            else:
                skipped = f"  ·  {row.issue_count} atlandı" if row.issue_count else ""
                label.configure(text=f"{row.count:,} öğe  ·  {format_size(row.size)}{skipped}", fg=TEXT)

    def _build_disk_page(self):
        tk.Label(self.disk_page, text=f"Windows sürücüsü ({self.system_drive})", bg=BG, fg=TEXT,
                 font=("Segoe UI", 19, "bold")).pack(anchor="w")
        cards = tk.Frame(self.disk_page, bg=BG)
        cards.pack(fill="x", pady=18)
        self.disk_cards = []
        for heading in ("Güncel boş alan", "Temizlik öncesi", "Temizlik sonrası"):
            card = tk.Frame(cards, bg=PANEL, padx=16, pady=14)
            card.pack(side="left", fill="both", expand=True, padx=(0, 6))
            tk.Label(card, text=heading, bg=PANEL, fg=MUTED).pack(anchor="w")
            value = tk.Label(card, text="—", bg=PANEL, fg=TEXT, font=("Segoe UI", 17, "bold"))
            value.pack(anchor="w", pady=(6, 4))
            bar = ttk.Progressbar(card, maximum=100)
            bar.pack(fill="x", pady=(0, 4))
            detail = tk.Label(card, bg=PANEL, fg=MUTED)
            detail.pack(anchor="w")
            self.disk_cards.append((value, bar, detail))
        self.disk_change = tk.Label(self.disk_page, bg=BG, fg=GREEN, font=("Segoe UI", 11, "bold"))
        self.disk_change.pack(anchor="w")
        tk.Label(self.disk_page, text="Boş alan değişimine diğer uygulamaların disk işlemleri de yansıyabilir.",
                 bg=BG, fg=MUTED).pack(anchor="w", pady=(6, 12))
        ttk.Button(self.disk_page, text="Güncel alanı yenile",
                   command=lambda: self._refresh_disk(self._disk_usage())).pack(anchor="w")

    def _disk_usage(self):
        try:
            return shutil.disk_usage(self.system_drive)
        except OSError:
            return None

    def _refresh_disk(self, current, comparison=None):
        samples = (current, *comparison) if comparison is not None else (current,)
        for (value, bar, detail), usage in zip(self.disk_cards, samples):
            value.configure(text=f"{format_size(usage.free)} boş" if usage else "Ölçülemedi")
            bar.configure(value=100 * usage.used / usage.total if usage and usage.total else 0)
            detail.configure(text=f"{format_size(usage.used)} / {format_size(usage.total)} dolu" if usage else "")
        if comparison is not None:
            before, after = comparison
            if before and after:
                change = after.free - before.free
                sign = "+" if change > 0 else ("−" if change < 0 else "")
                self.disk_change.configure(text=f"Boş alan değişimi  {sign}{format_size(abs(change))}")
            else:
                self.disk_change.configure(text="Boş alan değişimi ölçülemedi.")

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
        self.age_box.configure(state="disabled")
        for check, preview in self.area_controls.values():
            check.state(["disabled"])
            preview.state(["disabled"])
        for button in self.performance_buttons:
            button.state(["disabled"])
        self.cancel_button.configure(state="normal")
        self.progress.configure(mode="indeterminate")
        self.progress.start(12)

    def _end(self):
        self.busy = False
        self.cancel_button.configure(state="disabled")
        self.scan_button.configure(state="normal")
        self.clean_button.configure(state="normal")
        self.age_box.configure(state="readonly")
        for key, (check, preview) in self.area_controls.items():
            check.state(["!disabled"])
            preview.state(["!disabled"] if key in self.rows else ["disabled"])
        for button in self.performance_buttons:
            button.state(["!disabled"])
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
        keys = [key for key, selected in self.selected.items() if selected.get()]
        self.rows = {}
        self._refresh_areas()
        if not keys:
            self._end()
            self.total_label.configure(text="Alan seçilmedi")
            self.status.configure(text="Taramak istediğin alanları işaretle.")
            return
        self._begin()
        self.total_label.configure(text="Taranıyor…")
        self.status.configure(text=f"{self._days_value()} günlük filtreyle alanlar taranıyor…")
        cancel = self.cancel_event
        days = self._days_value()
        def worker():
            start = time.perf_counter()
            try:
                rows = scan_categories(keys, days, cancel, lambda key, count: self.events.put(("scan-progress", key, count)))
            except OSError as exc:
                self.events.put(("scan-error", str(exc)))
                return
            self.events.put(("scan-done", rows, cancel.is_set(), time.perf_counter() - start))
        threading.Thread(target=worker, daemon=True).start()

    def _poll_events(self):
        try:
            while True:
                event = self.events.get_nowait()
                if event[0] == "scan-progress":
                    self.status.configure(text=f"{LABELS[event[1]]}: {event[2]:,} dosya incelendi…")
                elif event[0] == "scan-done":
                    _, rows, cancelled, elapsed = event
                    if rows and not cancelled:
                        self.rows = rows
                        self._refresh_areas()
                        count = sum(row.count for row in rows.values())
                        size = sum(row.size for row in rows.values())
                        self.total_label.configure(text=f"{format_size(size)}  ·  {count} öğe")
                    self._end()
                    if cancelled:
                        self.total_label.configure(text="Tarama iptal edildi")
                    self.status.configure(text="Tarama iptal edildi; kısmi sonuçlar temizliğe açılmadı." if cancelled else f"{len(rows)} alan tarandı · {elapsed:.2f} sn")
                elif event[0] == "scan-error":
                    self._end()
                    self.total_label.configure(text="Tarama başarısız")
                    self.status.configure(text=event[1])
                elif event[0] == "performance-done":
                    _, render, result, error = event
                    self._end()
                    if self.cancel_event.is_set():
                        self.status.configure(text="Tarama iptal edildi.")
                    elif error is not None:
                        self.status.configure(text="İşlem tamamlanamadı.")
                        messagebox.showerror(APP_NAME, error, parent=self.root)
                    else:
                        render(result)
                        self.status.configure(text="Liste güncellendi.")
                elif event[0] == "clean-progress":
                    _, key, current, total = event
                    if total:
                        self.progress.stop()
                        self.progress.configure(mode="determinate", maximum=total, value=current)
                    self.status.configure(text=f"{LABELS[key]} temizleniyor: {current:,}/{total:,}…")
                elif event[0] == "clean-done":
                    _, outcomes, before, after = event
                    self._end()
                    self._refresh_disk(after, (before, after))
                    self._show_report(outcomes)
                    self.pages.select(self.disk_page)
                    self.scan()
        except queue.Empty:
            pass
        self.root.after(100, self._poll_events)

    def open_preview(self, key: str):
        if self.busy:
            return
        if not self.rows:
            messagebox.showinfo(APP_NAME, "Önizleme için önce tarama tamamlanmalı.", parent=self.root)
            return
        window = tk.Toplevel(self.root)
        window.title("PCleaner · Dosya önizlemesi")
        window.geometry("950x620")
        window.configure(bg=BG)
        window.transient(self.root)
        window.grab_set()
        frame = tk.Frame(window, bg=BG, padx=18, pady=14)
        frame.pack(fill="both", expand=True)
        top = tk.Frame(frame, bg=BG)
        top.pack(fill="x")
        tk.Label(top, text="TEMİZLENECEK DOSYALAR", bg=BG, fg=TEXT, font=("Segoe UI", 15, "bold")).pack(side="left")
        label_to_key = {label: key for key, label in LABELS.items()}
        preview_key = tk.StringVar(value=LABELS[key])
        selector = ttk.Combobox(top, textvariable=preview_key, state="readonly", width=34, values=[LABELS[area] for area in self.rows])
        selector.pack(side="right")
        info = tk.Label(frame, text="", bg=BG, fg=MUTED, anchor="w")
        info.pack(fill="x", pady=(8, 10))
        listing = tk.Frame(frame, bg=BG)
        table = ttk.Treeview(listing, columns=("state", "size", "date", "path"), show="headings", selectmode="extended")
        for col, title in (("state", "DURUM"), ("size", "BOYUT"), ("date", "DEĞİŞTİRİLME"), ("path", "DOSYA YOLU")):
            table.heading(col, text=title, anchor="w")
        table.column("state", width=90, anchor="center")
        table.column("size", width=90, anchor="e")
        table.column("date", width=145, anchor="w")
        table.column("path", width=590, anchor="w")
        table.tag_configure("excluded", foreground="#82918a")
        scroll = ttk.Scrollbar(listing, orient="vertical", command=table.yview)
        xscroll = ttk.Scrollbar(listing, orient="horizontal", command=table.xview)
        table.configure(yscrollcommand=scroll.set, xscrollcommand=xscroll.set)
        xscroll.pack(side="bottom", fill="x")
        scroll.pack(side="right", fill="y")
        table.pack(side="left", fill="both", expand=True)
        nav = tk.Frame(frame, bg=BG)
        nav.pack(side="bottom", fill="x", pady=(10, 0))
        listing.pack(fill="both", expand=True)
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
        if self.busy:
            return
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
            before = self._disk_usage()
            outcomes = {}
            for key in order:
                if cancel.is_set():
                    outcomes[key] = CleanResult(key, cancelled=True)
                else:
                    outcomes[key] = clean_category(key, self.rows[key], excluded, days, cancel, lambda k, current, total: self.events.put(("clean-progress", k, current, total)))
            self.events.put(("clean-done", outcomes, before, self._disk_usage()))
        threading.Thread(target=worker, daemon=True).start()

    def _show_report(self, outcomes: dict[str, CleanResult]):
        removed = sum(item.removed for item in outcomes.values())
        size = sum(item.size for item in outcomes.values())
        heading = "Temizlik durduruldu" if self.cancel_event.is_set() else "Temizlik tamamlandı"
        self.report_title.configure(text=f"{heading} · {removed:,} öğe · {format_size(size)}")
        text = self.report_text
        text.configure(state="normal")
        text.delete("1.0", "end")
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
