#!/usr/bin/env -S uv run --quiet --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["huggingface_hub>=1.32", "PySide6>=6.6", "psutil>=5.9", "markdown>=3.5"]
# ///
"""HF-Downloader: Qt 6 desktop front end for hffinish.

Download Hugging Face models into the hub cache (one at a time or a pasted
list, with a live progress bar and transfer speeds), finish downloads that
were interrupted, move complete models out of the cache into plain folders,
browse the Hub's model listing page by page, read a model's card with its
images, and browse the resulting library. All the cache logic lives in the
`hffinish` script next to this file; this module only drives it from a window
and shows what it does.

  HF-Downloader.cmd            start it on Windows (creates .venv on first run)
  python hf_downloader.py      start it with any Python that has the dependencies
  hf_downloader.py --selftest  open the window and quit (used by the build)
"""

from __future__ import annotations

import codecs
import importlib.machinery
import importlib.util
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import threading
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from types import ModuleType
from typing import Any

from PySide6.QtCore import QObject, QSettings, QStandardPaths, Qt, QThread, QTimer, QUrl, Signal, Slot
from PySide6.QtGui import QAction, QCloseEvent, QColor, QDesktopServices, QFont, QFontDatabase, QIcon, QImage, QPalette
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QSpinBox,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)

try:  # network throughput for the progress panel; the window works without it
    import psutil
except ImportError:  # pragma: no cover
    psutil = None  # type: ignore[assignment]
try:  # model cards: Markdown -> HTML with tables, fenced code and inline HTML
    import markdown as _markdown
except ImportError:  # pragma: no cover - Qt's own Markdown renderer is the fallback
    _markdown = None  # type: ignore[assignment]

APP_NAME = "HF-Downloader"
APP_VERSION = "1.2.0"
WINDOWS = os.name == "nt"
HUB_URL = "https://huggingface.co"


# --------------------------------------------------------------------------- hffinish


def _load_hffinish() -> ModuleType:
    """Import the hffinish script.

    In the PyInstaller build it is bundled as a regular module. From a checkout
    it is the extensionless file next to this one, loaded by path.
    """
    try:
        import hffinish  # type: ignore[import-not-found]

        return hffinish
    except ImportError:
        pass
    path = resource_path("hffinish")
    loader = importlib.machinery.SourceFileLoader("hffinish", str(path))
    spec = importlib.util.spec_from_loader("hffinish", loader)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["hffinish"] = module
    loader.exec_module(module)
    return module


def resource_path(rel: str) -> Path:
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    return base / rel


hff = _load_hffinish()


# --------------------------------------------------------------------------- repo references

HF_HOSTS = {"huggingface.co", "www.huggingface.co", "hf.co", "www.hf.co"}
REPO_PART_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
URL_RE = re.compile(r"^(?:[a-z][a-z0-9+.-]*://)?([^/?#]+)/(.*)$", re.IGNORECASE)


def parse_repo_ref(text: str) -> tuple[str, str]:
    """Turn any of the ways people write a Hugging Face model into (repo_id, revision).

    Accepted, among others:
      org/name                                   org/name/   org/name.git
      hf download org/name [--revision r]        huggingface-cli download org/name
      https://huggingface.co/org/name            https://huggingface.co/org/name?clone=true
      https://huggingface.co/org/name/tree/main  https://huggingface.co/org/name/blob/<rev>/file
      git clone https://huggingface.co/org/name  hf.co/org/name   huggingface.co/models/org/name
    Raises ValueError with a message meant for the user.
    """
    raw = text.strip().strip("\"'`")
    if not raw:
        raise ValueError("Enter a repo id (org/name) or a huggingface.co link.")
    tokens = [t.strip("\"'`") for t in raw.split()]
    tokens = [t for t in tokens if t]

    # command forms: the repo is the first non-option word after "download" or "clone";
    # a --revision anywhere on the line is honoured, other options are ignored
    lowered = [t.lower() for t in tokens]
    start = 0
    verb_found = False
    for verb in ("download", "clone"):
        if verb in lowered:
            start = lowered.index(verb) + 1
            verb_found = True
            break
    revision = ""
    candidate = ""
    extra: list[str] = []
    i = start
    while i < len(tokens):
        tok = tokens[i]
        if tok.startswith("-"):
            if tok in ("--revision", "-r") and i + 1 < len(tokens):
                revision = tokens[i + 1]
                i += 2
                continue
            if tok.startswith("--revision="):
                revision = tok.split("=", 1)[1]
            elif tok in ("--cache-dir", "--local-dir", "--include", "--exclude", "--token", "--repo-type") and i + 1 < len(tokens):
                i += 2  # option with a value
                continue
            i += 1
            continue
        if candidate:
            extra.append(tok)
        else:
            candidate = tok
        i += 1
    if not candidate:
        raise ValueError("Could not find a repo id in that text. Use the form org/name.")
    if extra and not verb_found:
        raise ValueError(f"'{raw}' looks like several words; enter one repo id (org/name) or one link.")

    # URL forms
    path = candidate
    if candidate.lower().startswith("hf://"):
        path = candidate[5:]
    else:
        m = URL_RE.match(candidate)
        if m:
            host = m.group(1).lower()
            if host in HF_HOSTS or host.endswith(".huggingface.co"):
                path = m.group(2)
            elif "://" in candidate:
                raise ValueError(f"{m.group(1)} is not huggingface.co; only Hugging Face links are supported.")
    path = path.split("?", 1)[0].split("#", 1)[0].strip("/")
    parts = [p for p in path.split("/") if p]
    if parts and parts[0].lower() == "models":
        parts = parts[1:]
    if parts and parts[0].lower() in ("datasets", "spaces"):
        raise ValueError(f"{parts[0]} repos are not supported here, only models.")
    if len(parts) < 2:
        raise ValueError(f"'{candidate}' is not a repo id of the form org/name.")
    org, name = parts[0], parts[1]
    if name.lower().endswith(".git"):
        name = name[:-4]
    if len(parts) >= 4 and parts[2] in ("tree", "blob", "resolve", "commit", "commits") and not revision:
        revision = parts[3]
    for part in (org, name):
        if not REPO_PART_RE.match(part):
            raise ValueError(f"'{part}' is not a valid part of a repo id.")
    return f"{org}/{name}", revision


def parse_repo_list(text: str) -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
    """Parse a pasted list, one repo per line, in any form parse_repo_ref accepts.

    Returns (accepted [(repo_id, revision)], rejected [(line, reason)]). Blank
    lines and lines starting with '#' are skipped; a repo listed twice is kept once.
    """
    accepted: list[tuple[str, str]] = []
    rejected: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for raw in text.splitlines():
        line = raw.strip().rstrip(",;")
        if not line or line.startswith("#"):
            continue
        try:
            ref = parse_repo_ref(line)
        except ValueError as exc:
            rejected.append((line, str(exc)))
            continue
        if ref not in seen:
            seen.add(ref)
            accepted.append(ref)
    return accepted, rejected


# --------------------------------------------------------------------------- transfer meter


@dataclass
class Transfer:
    """One reading of a running download, shown in the progress panel."""

    label: str  # "[2/5] org/name" or "org/name"
    done: int  # bytes of the repo in the cache
    total: int  # bytes the repo should have (0 = unknown)
    speed: float  # bytes/s landing in the cache (smoothed)
    down: float  # machine-wide network bytes/s received (psutil), -1 if unavailable
    up: float  # machine-wide network bytes/s sent, -1 if unavailable
    final: bool = False
    stalled: float = 0.0  # seconds since the last byte landed on disk (the heartbeat)
    alive: bool = True  # the downloading process still exists
    external: bool = False  # another process's download, watched rather than run by us
    pid: int = 0
    repo_id: str = ""


STALL_WARN_S = 20  # heartbeat: no bytes for this long is shown as a warning
STALL_ALARM_S = 90  # ... and for this long as a stall: our own downloads are restarted at this point
MAX_FAILED_RESTARTS = 5  # a download that keeps exiting with an error (not a stall) is given up after this many
MAX_CHECKSUM_ROUNDS = 3  # files whose hash does not match are fetched again, this many times at most


def heartbeat_text(t: "Transfer") -> tuple[str, str]:
    """(text, colour) of the heartbeat for a reading."""
    if t.final:
        return ("finished" if t.alive else "process ended", "#5f6368")
    if not t.alive:
        return ("process gone", "#c5221f")
    if t.stalled >= STALL_ALARM_S:
        return (f"stalled: no data for {int(t.stalled) // 60}:{int(t.stalled) % 60:02d}", "#c5221f")
    if t.stalled >= STALL_WARN_S:
        return (f"no data for {int(t.stalled)} s", "#b06000")
    return ("receiving data", "#1e8e3e")


def kill_tree(proc: subprocess.Popen) -> None:
    """End a download command and everything it spawned.

    `hf.exe` is a launcher: the download itself runs in a Python child (and on
    Windows often a grandchild). Ending only the launcher would leave that child
    downloading in the background, so the whole tree goes.
    """
    if psutil is not None:
        try:
            parent = psutil.Process(proc.pid)
            children = parent.children(recursive=True)
            for child in children:
                try:
                    child.terminate()
                except psutil.Error:
                    pass
            parent.terminate()
            _gone, alive = psutil.wait_procs([parent, *children], timeout=3)
            for p in alive:
                try:
                    p.kill()
                except psutil.Error:
                    pass
            return
        except psutil.Error:
            pass
    if WINDOWS:
        try:
            subprocess.run(
                ["taskkill", "/T", "/F", "/PID", str(proc.pid)],
                stdin=subprocess.DEVNULL,
                capture_output=True,
                timeout=15,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            return
        except (OSError, subprocess.SubprocessError):
            pass
    try:
        proc.terminate()
    except OSError:
        pass


def pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if psutil is not None:
        try:
            p = psutil.Process(pid)
            return p.is_running() and p.status() != psutil.STATUS_ZOMBIE
        except psutil.Error:
            return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def rate(bytes_per_s: float) -> str:
    """'85.3 MB/s (682 Mbit/s)' for the progress panel."""
    bits = bytes_per_s * 8
    if bits >= 1e9:
        bit_text = f"{bits / 1e9:.2f} Gbit/s"
    elif bits >= 1e6:
        bit_text = f"{bits / 1e6:.0f} Mbit/s"
    else:
        bit_text = f"{bits / 1e3:.0f} kbit/s"
    return f"{hff.human(bytes_per_s)}/s ({bit_text})"


class TransferMeter(threading.Thread):
    """Measures a download once a second: bytes of the repo on disk and network throughput.

    `hf download` prints its own progress bars, but their text is a poor source
    for a GUI (several bars at once, '\\r' redraws, hf_xet chunks). Watching the
    repo's blobs folder grow is exact and works for every downloader.
    """

    def __init__(
        self,
        emit,
        label: str,
        repo_id: str,
        cache_dir: Path,
        expected,  # int, or a callable returning one (run off the GUI thread)
        alive=None,  # callable -> bool; the meter ends by itself once it returns False
        external: bool = False,
        pid: int = 0,
        on_stall=None,  # callable(seconds) fired once when no byte has landed for STALL_ALARM_S
    ) -> None:
        super().__init__(daemon=True, name="transfer-meter")
        self.emit = emit
        self.label = label
        self.repo_id = repo_id
        self.repo_dir = cache_dir / f"models--{repo_id.replace('/', '--')}"
        self.expected = expected
        self.alive = alive
        self.external = external
        self.pid = pid
        self.on_stall = on_stall
        self._stop = threading.Event()
        self._final_on_stop = True

    def stop(self, final: bool = True) -> None:
        """End the meter. With final=False no closing reading is sent (the watcher lost interest)."""
        self._final_on_stop = final
        self._stop.set()

    def _reading(self, done: int, total: int, speed: float, down: float, up: float, stalled: float, alive: bool, final=False) -> Transfer:
        return Transfer(
            self.label, done, total, speed, down, up, final, stalled, alive, self.external, self.pid, self.repo_id
        )

    def _emit(self, reading: Transfer) -> None:
        try:
            self.emit(reading)
        except RuntimeError:  # the window (signal source) is gone: nobody to tell
            self._stop.set()

    def measure(self) -> int:
        total = 0
        try:
            names = os.listdir(self.repo_dir / "blobs")
        except OSError:
            return 0
        for fn in names:
            if fn.endswith(hff.STORE_SIDE_SUFFIXES):
                continue
            try:
                st = os.stat(self.repo_dir / "blobs" / fn)  # follows links into a shared store
            except OSError:
                continue
            if stat.S_ISREG(st.st_mode):
                total += st.st_size
        return total

    @staticmethod
    def _net():
        if psutil is None:
            return None
        try:
            return psutil.net_io_counters()
        except Exception:  # noqa: BLE001 - counters are a nicety
            return None

    def run(self) -> None:
        last_t = time.monotonic()
        last = self.measure()
        changed_t = last_t
        net = self._net()
        speed = 0.0
        alive = True
        total = 0
        stall_fired = False
        no_net = -1.0 if net is None else 0.0
        self._emit(self._reading(last, total, 0.0, no_net, no_net, 0.0, alive))
        if callable(self.expected):
            try:
                total = int(self.expected() or 0)
            except Exception:  # noqa: BLE001 - the bar just stays indeterminate
                total = 0
        else:
            total = int(self.expected or 0)
        while not self._stop.wait(1.0):
            now = time.monotonic()
            cur = self.measure()
            dt = max(now - last_t, 1e-3)
            if cur != last:
                changed_t = now
                stall_fired = False
            elif self.on_stall is not None and not stall_fired and now - changed_t >= STALL_ALARM_S:
                stall_fired = True
                try:
                    self.on_stall(now - changed_t)
                except Exception:  # noqa: BLE001 - the watchdog must never kill the meter
                    pass
            inst = max(cur - last, 0) / dt
            speed = inst if speed == 0 else 0.7 * speed + 0.3 * inst
            if now - changed_t >= 3:
                speed = 0.0  # nothing landed for a while: say so instead of decaying slowly
            down = up = -1.0
            net2 = self._net()
            if net is not None and net2 is not None:
                down = max(net2.bytes_recv - net.bytes_recv, 0) / dt
                up = max(net2.bytes_sent - net.bytes_sent, 0) / dt
            net = net2
            last_t, last = now, cur
            if self.alive is not None:
                try:
                    alive = bool(self.alive())
                except Exception:  # noqa: BLE001
                    alive = True
            self._emit(self._reading(cur, total, speed, down, up, now - changed_t, alive))
            if not alive:
                break
        if not alive or self._final_on_stop:
            self._emit(self._reading(self.measure(), total, 0.0, -1.0, -1.0, 0.0, alive, final=True))


def remove_stale_partials(repo_id: str, cache_dir: Path) -> int:
    """Drop *.incomplete leftovers of a download we just ended; the next run never appends to them."""
    freed = 0
    blobs = cache_dir / f"models--{repo_id.replace('/', '--')}" / "blobs"
    for part in blobs.glob("*.incomplete"):
        try:
            freed += part.stat().st_size
            part.unlink()
        except OSError:
            pass
    return freed


# --------------------------------------------------------------------------- checksums


@dataclass
class FileCheck:
    path: str
    size: int
    algo: str  # "sha256" (LFS files) or "git-sha1" (small files stored in git)
    expected: str  # from the Hub, before the download
    after_download: str = ""  # hashed in the cache once the download finished
    after_move: str = ""  # hashed again in the library after the move
    note: str = ""

    def stage_ok(self, stage: str) -> bool | None:
        """True/False when that stage was hashed, None when it was not."""
        got = getattr(self, stage)
        if not got or not self.expected:
            return None
        return got == self.expected


@dataclass
class ChecksumReport:
    repo_id: str
    commit: str = ""
    files: list[FileCheck] = field(default_factory=list)
    notes: dict[str, str] = field(default_factory=dict)  # stage -> what happened (errors, "not moved", ...)
    rounds: int = 0  # how many times mismatching files had to be fetched again
    restarts: int = 0  # how many times the download itself was restarted (stalls, exits)

    def stage_counts(self, stage: str) -> tuple[int, int, int]:
        """(matching, mismatching, not hashed) for a stage."""
        ok = bad = none = 0
        for f in self.files:
            r = f.stage_ok(stage)
            if r is None:
                none += 1
            elif r:
                ok += 1
            else:
                bad += 1
        return ok, bad, none

    def stage_verdict(self, stage: str) -> tuple[bool | None, str]:
        label = {"expected": "1. Before download: checksums from the Hub", "after_download": "2. After download: files hashed in the cache", "after_move": "3. After move: files hashed in the library"}[stage]
        if stage == "expected":
            known = sum(1 for f in self.files if f.expected)
            if not self.files:
                return None, f"{label}: {self.notes.get('expected', 'no file list')}"
            return (known == len(self.files)), f"{label}: {known} of {len(self.files)} files have a checksum on the Hub"
        ok, bad, none = self.stage_counts(stage)
        note = self.notes.get(stage, "")
        if note and not ok and not bad:
            return None, f"{label}: {note}"
        text = f"{label}: {ok} match"
        if bad:
            text += f", {bad} MISMATCH"
        if none:
            text += f", {none} not hashed"
        if note:
            text += f" ({note})"
        return (bad == 0 and ok > 0), text

    @property
    def all_ok(self) -> bool:
        return all(self.stage_verdict(s)[0] for s in ("expected", "after_download", "after_move"))


def expected_checksums(repo_id: str, revision: str) -> tuple[str, list[FileCheck]]:
    """The Hub's checksums for every file at that revision: sha256 for LFS files, git blob sha1 otherwise."""
    api = hff.HfApi()
    info = api.model_info(repo_id, revision=revision or None)
    commit = info.sha or ""
    files: list[FileCheck] = []
    for entry in api.list_repo_tree(repo_id, revision=commit or revision or None, recursive=True, expand=True):
        if not isinstance(entry, hff.RepoFile):
            continue
        lfs = getattr(entry, "lfs", None)
        if lfs is not None and getattr(lfs, "sha256", None):
            files.append(FileCheck(entry.path, int(entry.size), "sha256", lfs.sha256.lower()))
        else:
            files.append(FileCheck(entry.path, int(entry.size), "git-sha1", (getattr(entry, "blob_id", "") or "").lower()))
    files.sort(key=lambda f: f.path.lower())
    return commit, files


def hash_file(path: Path, algo: str, size: int, progress=None) -> str:
    import hashlib

    if algo == "git-sha1":
        h = hashlib.sha1()
        h.update(b"blob %d\0" % size)
    else:
        h = hashlib.sha256()
    done = 0
    with open(path, "rb") as fh:
        while True:
            chunk = fh.read(8 << 20)
            if not chunk:
                break
            h.update(chunk)
            done += len(chunk)
            if progress is not None:
                progress(done)
    return h.hexdigest()


def hash_tree(root: Path, files: list[FileCheck], stage: str, w: "Worker", repo_id: str) -> list[FileCheck]:
    """Hash every file under root into the given stage of the report; returns the mismatching ones."""
    bad: list[FileCheck] = []
    total = sum(f.size for f in files)
    done_total = 0
    for n, f in enumerate(files, 1):
        if w.cancelled:
            raise KeyboardInterrupt
        p = root / f.path
        label = f"hashing {n}/{len(files)} {f.path}"
        w.repo_update.emit(repo_id, "verifying", label, None)
        if not p.is_file():
            f.note = f"{stage}: file missing"
            setattr(f, stage, "missing")
            bad.append(f)
            continue
        try:
            start = done_total

            def prog(b: int, start=start) -> None:
                w.progress.emit(f"{repo_id}: {label}  {hff.human(start + b)} / {hff.human(total)}")

            digest = hash_file(p, f.algo, f.size, prog)
        except OSError as exc:
            digest = "unreadable"
            f.note = f"{stage}: {exc}"
        setattr(f, stage, digest)
        done_total += f.size
        mark_ok = f.stage_ok(stage)
        tick = "OK, match" if mark_ok else ("no expected value" if mark_ok is None else "MISMATCH")
        w.line.emit(
            f"  {stage.replace('_', ' ')} {f.algo} {f.path}: expected {f.expected or '-'}  received {digest}  -> {tick}"
        )
        if mark_ok is False:
            bad.append(f)
    w.progress.emit("")
    return bad


def drop_cached_files(repo_id: str, cache_dir: Path, snapshot: Path, files: list[FileCheck]) -> None:
    """Remove mismatching files (pointer and blob) so the next download fetches them again."""
    for f in files:
        chain = hff.link_chain(snapshot / f.path)
        for p in chain:
            try:
                if p.is_symlink() or p.is_file():
                    p.unlink()
            except OSError:
                pass
        if chain:
            hff.remove_store_side_files(chain[-1], cache_dir)


def cached_snapshot(repo_id: str, cache_dir: Path, commit: str) -> Path | None:
    repo_dir = cache_dir / f"models--{repo_id.replace('/', '--')}"
    if commit and (repo_dir / "snapshots" / commit).is_dir():
        return repo_dir / "snapshots" / commit
    try:
        ref = (repo_dir / "refs" / "main").read_text().strip()
        if (repo_dir / "snapshots" / ref).is_dir():
            return repo_dir / "snapshots" / ref
    except OSError:
        pass
    snaps = sorted((repo_dir / "snapshots").glob("*"), key=lambda p: p.stat().st_mtime, reverse=True) if (repo_dir / "snapshots").is_dir() else []
    return snaps[0] if snaps else None


def cached_expected_size(repo_id: str, cache_dir: Path) -> int:
    """Bytes the cached revision of a repo should hold, from the on-disk file list (0 if none)."""
    repo_dir = cache_dir / f"models--{repo_id.replace('/', '--')}"
    try:
        for tree_file in (repo_dir / "trees").glob("*.json"):
            data = json.loads(tree_file.read_text())
            if data.get("format_version") == 1:
                return sum(int(e["size"]) for e in data["files"].values())
    except (OSError, ValueError, KeyError, TypeError):
        pass
    return 0


def expected_size(repo_id: str, revision: str) -> int:
    """Bytes the repo holds at that revision according to the Hub (0 if it cannot be asked)."""
    try:
        api = hff.HfApi()
        return sum(
            int(e.size)
            for e in api.list_repo_tree(repo_id, revision=revision or None, recursive=True)
            if isinstance(e, hff.RepoFile)
        )
    except Exception:  # noqa: BLE001 - the bar just stays indeterminate
        return 0


# --------------------------------------------------------------------------- options


@dataclass
class Options:
    cache_dir: str = ""
    dest: str = ""
    layout: str = "flat"
    filters: list[str] = field(default_factory=list)
    dry_run: bool = False
    no_download: bool = False
    no_move: bool = False
    merge: bool = False
    checksum: bool = True  # verify every file's hash against the Hub before moving it
    wait: bool = False
    poll: int = 30
    no_queue: bool = False

    def argv(self, filters: list[str] | None = None) -> list[str]:
        argv = list(self.filters if filters is None else filters)
        if self.cache_dir:
            argv += ["--cache-dir", self.cache_dir]
        if self.dest:
            argv += ["--dest", self.dest]
        argv += ["--layout", self.layout]
        for flag, on in (
            ("--dry-run", self.dry_run),
            ("--no-download", self.no_download),
            ("--no-move", self.no_move),
            ("--merge", self.merge),
            ("--checksum", self.checksum),
            ("--no-queue", self.no_queue),
        ):
            if on:
                argv.append(flag)
        if self.wait:
            argv += ["--wait", "--poll", str(self.poll)]
        return argv

    def cache_path(self) -> Path:
        return Path(self.cache_dir or hff.constants.HF_HUB_CACHE).expanduser()

    def dest_path(self) -> Path:
        return Path(self.dest or hff.DEFAULT_DEST).expanduser()


@dataclass
class QueueItem:
    """A download waiting for a slot, or remembered across restarts."""

    repo_id: str
    revision: str = ""
    mode: str = "download"  # "download": fetch it; "resume": pick a cached, unfinished one up

    def as_dict(self) -> dict[str, str]:
        return {"repo_id": self.repo_id, "revision": self.revision, "mode": self.mode}

    @classmethod
    def from_dict(cls, d: dict) -> "QueueItem":
        return cls(str(d.get("repo_id", "")), str(d.get("revision", "")), str(d.get("mode", "download")))


# --------------------------------------------------------------------------- worker


class _LineSplitter:
    """File-like sink: '\\n' ends a log line, '\\r' is a progress update (tqdm style)."""

    def __init__(self, worker: "Worker") -> None:
        self.worker = worker
        self.buf = ""
        self.last_progress = ""

    def write(self, text: str) -> int:
        if not text:
            return 0
        self.buf += text
        while True:
            cuts = [i for i in (self.buf.find("\n"), self.buf.find("\r")) if i >= 0]
            if not cuts:
                break
            i = min(cuts)
            seg, sep, self.buf = self.buf[:i], self.buf[i], self.buf[i + 1 :]
            if sep == "\n":
                line = seg.rstrip()
                if not line and self.last_progress:
                    line = self.last_progress  # "\r...100%\n": keep the final state in the log
                self.last_progress = ""
                if line:
                    self.worker.line.emit(line)
                self.worker.progress.emit("")
            elif seg.strip():
                self.last_progress = seg.rstrip()
                self.worker.progress.emit(self.last_progress)
        return len(text)

    def flush(self) -> None:
        if self.buf.strip():
            self.worker.line.emit(self.buf.rstrip())
        self.buf = ""

    def isatty(self) -> bool:
        return False


# hffinish's `log`, `run_download` and `process` and the process's stdout/stderr
# are replaced once, by dispatchers that look up the Worker of the calling
# thread. Several workers can then run at the same time, each receiving only
# its own messages, output and status changes.
_current = threading.local()
_ORIG: dict[str, Any] = {}


def _hook_log(repo, msg: str) -> None:
    w = getattr(_current, "worker", None)
    if w is None:
        return _ORIG["log"](repo, msg)
    w._log(repo, msg)


def _hook_run_download(repo, cmd: list[str], env: dict[str, str]) -> int:
    w = getattr(_current, "worker", None)
    if w is None:
        return _ORIG["run_download"](repo, cmd, env)
    return w._run_download(repo, cmd, env)


def _hook_process(repo, *args) -> None:
    w = getattr(_current, "worker", None)
    if w is None:
        return _ORIG["process"](repo, *args)
    return w._process(repo, *args)


class _ThreadStdout:
    """sys.stdout/sys.stderr replacement: a worker thread's output goes to its own sink."""

    encoding = "utf-8"
    errors = "replace"

    def __init__(self, orig) -> None:
        self.orig = orig  # None under pythonw

    def write(self, text: str) -> int:
        sink = getattr(_current, "sink", None)
        if sink is not None:
            return sink.write(text)
        if self.orig is not None:
            return self.orig.write(text)
        return len(text)

    def flush(self) -> None:
        sink = getattr(_current, "sink", None)
        if sink is not None:
            sink.flush()
        elif self.orig is not None:
            self.orig.flush()

    def isatty(self) -> bool:
        return False

    def fileno(self) -> int:
        if self.orig is None:
            raise OSError("no console")
        return self.orig.fileno()


def install_hooks() -> None:
    if _ORIG:
        return
    _ORIG.update(log=hff.log, run_download=hff.run_download, process=hff.process)
    hff.log, hff.run_download, hff.process = _hook_log, _hook_run_download, _hook_process
    sys.stdout = _ThreadStdout(sys.stdout)
    sys.stderr = _ThreadStdout(sys.stderr)


class Worker(QThread):
    """Runs one job (a callable taking the worker) off the GUI thread.

    Every hffinish message, every byte of download output and every status
    change made on this thread arrives here as a signal (see install_hooks).
    Several workers may run at once; `kind` says how they mix: "download"
    jobs (one repo each) run side by side, an "exclusive" job (a pass over
    the whole cache) runs alone.
    """

    line = Signal(str)  # a finished log line
    progress = Signal(str)  # transient progress text ("" clears it)
    repos_found = Signal(list)  # repo ids the job is about to work on
    repo_update = Signal(str, str, str, object)  # repo id, status, note, total bytes or None
    transfer = Signal(object)  # a Transfer reading of the running download
    external = Signal(str, str)  # repo id, reason: another process is downloading this repo
    checksums = Signal(object)  # a ChecksumReport once a download is complete and verified
    done = Signal(int)  # exit code

    def __init__(self, job, cache_dir: Path, parent=None) -> None:
        super().__init__(parent)
        self.job = job
        self.cache_dir = cache_dir
        self.kind = "download"  # or "exclusive"
        self.repo_ids: list[str] = []  # repos this job is about (for the per-repo Stop)
        self.ctx: tuple | None = None  # ("download", repo_ids, finish_after) or ("verify", repo_ids)
        self.current_repo = ""  # repo whose download the meter is measuring right now
        self.rc: int | None = None
        self.restarts = 0  # how often this job's download had to be restarted
        self._cancel = threading.Event()
        self._stall_restart = False  # the heartbeat killed the download because it stalled
        self._proc: subprocess.Popen | None = None
        self._lock = threading.Lock()
        self._meter: TransferMeter | None = None

    # -- control

    def cancel(self) -> None:
        self._cancel.set()
        with self._lock:
            proc = self._proc
        if proc is not None and proc.poll() is None:
            kill_tree(proc)

    @property
    def cancelled(self) -> bool:
        return self._cancel.is_set()

    # -- progress meter

    def start_meter(self, repo_id: str, expected: int) -> None:
        self.stop_meter()
        self.current_repo = repo_id
        self._meter = TransferMeter(
            self.transfer.emit,
            repo_id,
            repo_id,
            self.cache_dir,
            expected or (lambda: cached_expected_size(repo_id, self.cache_dir)),
            alive=self._download_alive,
            on_stall=self._on_stall,
        )
        self._meter.start()

    def _download_alive(self) -> bool:
        """Heartbeat for our own download: the child process is still there (in-process
        downloads have no child, so they count as alive until the job returns)."""
        with self._lock:
            proc = self._proc
        return proc is None or proc.poll() is None

    def _on_stall(self, stalled: float) -> None:
        """Heartbeat watchdog (meter thread): nothing landed for STALL_ALARM_S, so end the
        download command; run_with_restarts then starts it again."""
        with self._lock:
            proc = self._proc
        if proc is None or proc.poll() is not None or self.cancelled:
            return
        self._stall_restart = True
        self.line.emit(f"heartbeat: no data for {int(stalled)} s, restarting the download")
        kill_tree(proc)

    def _sleep(self, seconds: float) -> None:
        """Wait between restarts, but wake up at once when cancelled."""
        if self._cancel.wait(seconds):
            raise KeyboardInterrupt

    def run_with_restarts(self, repo_id: str, cmd: list[str], env: dict[str, str], expected: int) -> int:
        """Run a download command until it succeeds.

        A stall (no bytes for STALL_ALARM_S) ends the command and starts it again
        as often as needed; an exit with an error is retried MAX_FAILED_RESTARTS
        times. Complete files are skipped by the downloader, stale partial files
        are dropped first, and the wait between attempts grows from 5 s to 60 s.
        """
        attempt = 0
        failures = 0
        while True:
            self._stall_restart = False
            self.start_meter(repo_id, expected)
            try:
                rc = self.stream(cmd, env)
            finally:
                self.stop_meter()
            if rc == 0:
                return 0
            if self.cancelled:
                raise KeyboardInterrupt
            if self._stall_restart:
                why = "stalled"
            else:
                failures += 1
                why = f"exited with code {rc}"
                if failures > MAX_FAILED_RESTARTS:
                    self.line.emit(f"{repo_id}: download {why} {failures} times, giving up (Resume tries again)")
                    return rc
            attempt += 1
            self.restarts += 1
            wait = min(5 * 2 ** min(attempt - 1, 4), 60)
            freed = remove_stale_partials(repo_id, self.cache_dir)
            note = f"{why}; restart {attempt} in {wait} s"
            if freed:
                note += f", {hff.human(freed)} of partial files dropped"
            self.line.emit(f"{repo_id}: download {note} (complete files are kept)")
            self.repo_update.emit(repo_id, "restarting", note, None)
            self._sleep(wait)
            self.repo_update.emit(repo_id, "downloading", f"restart {attempt}", None)

    def stop_meter(self) -> None:
        if self._meter is not None:
            self._meter.stop()
            self._meter.join(3)
            self._meter = None

    # -- thread body

    def run(self) -> None:
        install_hooks()
        sink = _LineSplitter(self)
        _current.worker = self
        _current.sink = sink
        rc = 1
        try:
            try:
                rc = int(self.job(self) or 0)
            except KeyboardInterrupt:
                rc = 130
                self.line.emit("stopped")
            except Exception:  # noqa: BLE001 - show it in the log instead of dying silently
                self.line.emit(traceback.format_exc().rstrip())
                rc = 1
        finally:
            sink.flush()
            self.stop_meter()
            _current.worker = None
            _current.sink = None
        self.rc = rc
        self.progress.emit("")
        self.done.emit(rc)

    # -- hooks installed into hffinish

    def _log(self, repo, msg: str) -> None:
        if self.cancelled:
            raise KeyboardInterrupt
        stamp = time.strftime("%H:%M:%S")
        self.line.emit(f"{stamp} [{repo.repo_id}] {msg}" if repo else f"{stamp} {msg}")
        if repo is not None and msg.startswith("being downloaded by another process"):
            self.repo_update.emit(repo.repo_id, "downloading", msg, repo.total_bytes or None)
            self.external.emit(repo.repo_id, msg)

    def _process(self, repo, *args) -> None:
        self.repo_update.emit(repo.repo_id, "checking", "", None)
        try:
            _ORIG["process"](repo, *args)
        finally:
            self.repo_update.emit(repo.repo_id, repo.status or "checked", repo.note, repo.total_bytes or None)

    def _run_download(self, repo, cmd: list[str], env: dict[str, str]) -> int:
        if repo is None:
            return self.stream(cmd, env)
        self.repo_update.emit(repo.repo_id, "downloading", os.path.basename(cmd[0]), repo.total_bytes or None)
        return self.run_with_restarts(repo.repo_id, cmd, env, repo.total_bytes)

    # -- helpers for jobs

    def stream(self, cmd: list[str], env: dict[str, str] | None = None) -> int:
        """Run a command, feeding its output through the log, and return its exit code."""
        if self.cancelled:
            raise KeyboardInterrupt
        env = dict(env if env is not None else os.environ)
        env.setdefault("PYTHONUTF8", "1")
        env.setdefault("PYTHONIOENCODING", "utf-8")
        flags = subprocess.CREATE_NO_WINDOW if WINDOWS else 0
        proc = subprocess.Popen(
            cmd,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            creationflags=flags,
        )
        with self._lock:
            self._proc = proc
        decoder = codecs.getincrementaldecoder("utf-8")("replace")
        assert proc.stdout is not None
        try:
            while True:
                chunk = proc.stdout.read1(4096)
                if not chunk:
                    break
                sys.stdout.write(decoder.decode(chunk))
            sys.stdout.write(decoder.decode(b"", final=True))
            rc = proc.wait()
        finally:
            with self._lock:
                self._proc = None
        if self.cancelled:
            raise KeyboardInterrupt
        return rc


# --------------------------------------------------------------------------- jobs


def finish_job(opts: Options):
    """Check, resume and move every cached model (what `hffinish` does)."""

    def job(w: Worker) -> int:
        cache = opts.cache_path()
        if cache.is_dir():
            ids = []
            for info in hff.scan_cache(cache):
                if info.repo_type != "model":
                    continue
                if opts.filters and not any(f.lower() in info.repo_id.lower() for f in opts.filters):
                    continue
                ids.append(info.repo_id)
            w.repos_found.emit(sorted(ids, key=str.lower))
        return hff.main(opts.argv())

    return job


def _fetch(w: Worker, repo_id: str, revision: str, opts: Options, expected: int, resume: bool) -> int:
    """Get the repo's files into the cache: `hf download` (restarted as needed) or, for a
    repo already in the cache, hffinish's resume pass without the move."""
    if resume:
        fin = Options(**{**opts.__dict__, "filters": [repo_id], "no_move": True, "no_download": False, "dry_run": False, "checksum": False})
        w.line.emit("resuming: hffinish " + " ".join(fin.argv()))
        return hff.main(fin.argv())
    env = hff.tool_env()
    hf = shutil.which("hf", path=env["PATH"]) or shutil.which("hf")
    if hf:
        cmd = [hf, "download", repo_id]
        if revision:
            cmd += ["--revision", revision]
        if opts.cache_dir:
            cmd += ["--cache-dir", opts.cache_dir]
        w.line.emit("running: " + " ".join(cmd))
        return w.run_with_restarts(repo_id, cmd, env, expected)
    from huggingface_hub import snapshot_download

    w.line.emit("hf CLI not found on PATH, downloading in-process (cannot be stopped midway)")
    w.start_meter(repo_id, expected)
    try:
        snapshot_download(repo_id, revision=revision or None, cache_dir=opts.cache_dir or None)
        return 0
    except Exception as exc:  # noqa: BLE001
        w.line.emit(f"download failed: {exc}")
        return 1
    finally:
        w.stop_meter()


def download_job(repo_id: str, revision: str, opts: Options, finish_after: bool, resume: bool = False):
    """Download (or resume) one repo and verify it three times over.

    1. before: the Hub's checksums for every file at the revision,
    2. after the download: every file hashed in the cache; mismatching files are
       dropped and fetched again (up to MAX_CHECKSUM_ROUNDS),
    3. after the move: every file hashed again in the library.
    The report goes to the window, which shows it in a pop-up.
    """

    def job(w: Worker) -> int:
        w.repos_found.emit([repo_id])
        report = ChecksumReport(repo_id)
        cache = opts.cache_path()

        # 1. what the Hub says every file should hash to
        w.repo_update.emit(repo_id, "checking", "asking the Hub for the file list and checksums", None)
        try:
            report.commit, report.files = expected_checksums(repo_id, revision)
            known = sum(1 for f in report.files if f.expected)
            w.line.emit(
                f"{repo_id}: {len(report.files)} files, {hff.human(sum(f.size for f in report.files))} at "
                f"{report.commit[:12] or revision or 'main'}; checksums known for {known} (before download)"
            )
            for f in report.files:
                w.line.emit(f"  expected {f.algo} {f.path}: {f.expected or '-'}  (received hash is logged next to it after the download)")
        except Exception as exc:  # noqa: BLE001 - offline, gated, typo
            report.notes["expected"] = f"could not get the file list: {type(exc).__name__}: {exc}"
            w.line.emit(f"{repo_id}: {report.notes['expected']}")
        expected_bytes = sum(f.size for f in report.files)
        w.repo_update.emit(repo_id, "downloading", "", expected_bytes or None)

        # 2. download, hash in the cache, fetch mismatching files again
        rc = 1
        for round_no in range(1, MAX_CHECKSUM_ROUNDS + 1):
            rc = _fetch(w, repo_id, revision, opts, expected_bytes, resume and round_no == 1)
            if rc != 0:
                w.repo_update.emit(repo_id, "error", f"download exited with code {rc}", None)
                report.notes["after_download"] = f"download did not finish (exit {rc})"
                report.restarts = w.restarts
                w.checksums.emit(report)
                return rc
            snapshot = cached_snapshot(repo_id, cache, report.commit)
            if snapshot is None:
                report.notes["after_download"] = "no snapshot in the cache"
                break
            if not report.files:
                report.notes["after_download"] = "nothing to compare against"
                break
            w.line.emit(f"{repo_id}: download complete, hashing {len(report.files)} files in the cache (after download)")
            bad = hash_tree(snapshot, report.files, "after_download", w, repo_id)
            if not bad:
                w.line.emit(f"{repo_id}: all {len(report.files)} files match the Hub's checksums")
                break
            report.rounds = round_no
            names = ", ".join(f.path for f in bad[:5]) + (" ..." if len(bad) > 5 else "")
            if round_no == MAX_CHECKSUM_ROUNDS:
                report.notes["after_download"] = f"{len(bad)} file(s) still wrong after {round_no} downloads: {names}"
                w.line.emit(f"{repo_id}: {report.notes['after_download']}")
                w.repo_update.emit(repo_id, "error", report.notes["after_download"], None)
                report.restarts = w.restarts
                w.checksums.emit(report)
                return 1
            w.line.emit(f"{repo_id}: {len(bad)} file(s) do not match ({names}); dropping them and downloading again")
            drop_cached_files(repo_id, cache, snapshot, bad)
        w.repo_update.emit(repo_id, "downloaded", "in the cache, checksums verified", None)
        report.restarts = w.restarts

        # 3. move to the library and hash once more there
        if not finish_after:
            report.notes["after_move"] = "left in the cache (not moved)"
            w.checksums.emit(report)
            return 0
        w.line.emit("")
        fin = Options(**{**opts.__dict__, "filters": [repo_id], "checksum": False})  # hashed a moment ago
        rc = hff.main(fin.argv())
        snap_before = cached_snapshot(repo_id, cache, report.commit)
        dest = opts.dest_path() / (repo_id if opts.layout == "nested" else repo_id.split("/")[-1])
        if snap_before is None and dest.is_dir() and report.files:
            w.line.emit(f"{repo_id}: moved, hashing {len(report.files)} files in the library (after move)")
            bad = hash_tree(dest, report.files, "after_move", w, repo_id)
            if bad:
                report.notes["after_move"] = f"{len(bad)} file(s) differ after the move"
                w.repo_update.emit(repo_id, "error", report.notes["after_move"], None)
                rc = 1
            else:
                w.line.emit(f"{repo_id}: all {len(report.files)} files still match in the library")
        elif rc != 0 or snap_before is not None:
            report.notes["after_move"] = "not moved (see the log)"
        else:
            report.notes["after_move"] = "nothing to compare against"
        w.checksums.emit(report)
        return rc

    return job


# --------------------------------------------------------------------------- library


@dataclass
class LibraryEntry:
    name: str
    path: Path
    state: str  # ready | moving | in cache | incomplete | unverified
    files: int = 0
    size: int = 0
    modified: float = 0.0
    repo_id: str = ""  # set for cache entries: what Verify / Resume act on
    expected_files: int = 0  # cache entries: what the file list says the snapshot should hold
    expected_size: int = 0
    note: str = ""

    @property
    def in_cache(self) -> bool:
        return bool(self.repo_id)


class LibraryScanner(QThread):
    """Lists the models in the destination folder (and what still sits in the cache).

    Cache entries are checked file by file against the file list huggingface_hub
    stores next to the snapshot (trees/<commit>.json), so a download that stopped
    shows up as "incomplete" without touching the network. Repos without that
    list are "unverified" until a Verify run fetches it.
    """

    found = Signal(list)

    def __init__(self, dest: Path, cache: Path, layout: str, parent=None) -> None:
        super().__init__(parent)
        self.dest = dest
        self.cache = cache
        self.layout = layout

    def run(self) -> None:
        entries: list[LibraryEntry] = []
        try:
            if self.dest.is_dir():
                for child in sorted(self.dest.iterdir(), key=lambda p: p.name.lower()):
                    if not child.is_dir():
                        continue
                    if child.name.startswith(hff.STAGING_PREFIX):
                        entries.append(self._entry(child.name[len(hff.STAGING_PREFIX) :], child, "moving"))
                    elif child.name.startswith("."):
                        continue
                    elif any(p.is_file() for p in child.iterdir()):
                        entries.append(self._entry(child.name, child, "ready"))
                    else:  # an org folder of the nested layout
                        for sub in sorted(child.iterdir(), key=lambda p: p.name.lower()):
                            if not sub.is_dir():
                                continue
                            if sub.name.startswith(hff.STAGING_PREFIX):
                                name = sub.name[len(hff.STAGING_PREFIX) :]
                                entries.append(self._entry(f"{child.name}/{name}", sub, "moving"))
                            elif not sub.name.startswith("."):
                                entries.append(self._entry(f"{child.name}/{sub.name}", sub, "ready"))
            if self.cache.is_dir():
                for info in hff.scan_cache(self.cache):
                    if info.repo_type != "model":
                        continue
                    rev = hff.pick_revision(info)
                    if rev is None:
                        continue
                    entries.append(cache_entry(info, rev, self.dest, self.cache, self.layout))
        except OSError:
            pass
        self.found.emit(entries)

    @staticmethod
    def _entry(name: str, path: Path, state: str) -> LibraryEntry:
        return folder_entry(name, path, state)


def folder_entry(name: str, path: Path, state: str) -> LibraryEntry:
    files = size = 0
    modified = 0.0
    for root, _dirs, names in os.walk(path):
        for fn in names:
            try:
                st = os.stat(os.path.join(root, fn))
            except OSError:
                continue
            files += 1
            size += st.st_size
            modified = max(modified, st.st_mtime)
    return LibraryEntry(name, path, state, files, size, modified)


def download_in_progress(info, cache: Path) -> str:
    """Cheap local version of hffinish.active_reason: locks and fresh partial files only
    (no process listing, which costs a PowerShell start per repo on Windows)."""
    for lock in (cache / ".locks" / info.repo_path.name).glob("*.lock"):
        if hff.lock_is_held(lock):
            return "download lock held"
    now = time.time()
    for part in (info.repo_path / "blobs").glob("*.incomplete"):
        try:
            age = now - part.stat().st_mtime
        except OSError:
            continue
        if age < hff.ACTIVE_WINDOW_S:
            return f"partial file written {int(age)}s ago"
    return ""


def cache_entry(info, rev, dest: Path, cache: Path, layout: str) -> LibraryEntry:
    """What the cache holds of one repo, checked against the on-disk file list (no network)."""
    entry = folder_entry(info.repo_id, rev.snapshot_path, "in cache")
    entry.repo_id = info.repo_id
    repo = hff.Repo(info, rev)
    tree_file = info.repo_path / "trees" / f"{rev.commit_hash}.json"
    tree: dict[str, int] | None = None
    try:
        data = json.loads(tree_file.read_text())
        if data.get("format_version") == 1:
            tree = {p: int(e["size"]) for p, e in data["files"].items()}
    except (OSError, ValueError, KeyError, TypeError):
        tree = None
    if tree is None:
        repo.partials = sorted((info.repo_path / "blobs").glob("*.incomplete"))
        entry.state = "unverified"
        entry.note = "no file list on disk; select it and click Verify"
        if repo.partials:
            entry.note = f"{len(repo.partials)} partial file(s) ({hff.human(repo.partial_bytes)}); " + entry.note
        return entry
    repo.tree = tree
    hff.check(repo, repo.staging(dest, layout))
    entry.expected_files = len(tree)
    entry.expected_size = repo.total_bytes
    todo = repo.missing + repo.bad
    if not todo:
        entry.state = "in cache"
        entry.note = "complete; Finish and move puts it in the library"
        return entry
    need = sum(tree[r] for r in todo)
    entry.files = len(tree) - len(todo)
    entry.size = max(entry.expected_size - need, 0)
    active = download_in_progress(info, cache)
    entry.state = "downloading" if active else "incomplete"
    entry.note = f"{len(repo.missing)} missing, {len(repo.bad)} bad of {len(tree)} files, {hff.human(need)} still to fetch"
    if repo.partials:
        entry.note += f"; {len(repo.partials)} partial file(s) ({hff.human(repo.partial_bytes)})"
    if active:
        entry.note = f"another process is downloading it ({active}); " + entry.note
    return entry


@dataclass
class LocalState:
    """Whether a repo is already on this machine: in the library, in the cache, or not at all."""

    state: str = ""  # "" | in library | in cache | incomplete | downloading | unverified
    path: Path | None = None
    note: str = ""

    @property
    def downloaded(self) -> bool:
        """Completely on disk: a Download would fetch nothing new."""
        return self.state in ("in library", "in cache")


class LocalIndex:
    """Looks repos up on disk. Built once per page / refresh, off the GUI thread."""

    def __init__(self, dest: Path, cache: Path, layout: str) -> None:
        self.dest = dest
        self.cache = cache
        self.layout = layout
        self.cached: dict[str, tuple[Any, Any]] = {}
        try:
            if cache.is_dir():
                for info in hff.scan_cache(cache):
                    if info.repo_type != "model":
                        continue
                    rev = hff.pick_revision(info)
                    if rev is not None:
                        self.cached[info.repo_id.lower()] = (info, rev)
        except OSError:
            pass

    def lookup(self, repo_id: str) -> LocalState:
        org, _, name = repo_id.partition("/")
        for folder in (self.dest / name, self.dest / org / name):  # both layouts, whatever is set now
            try:
                if folder.is_dir() and any(p.is_file() for p in folder.rglob("*")):
                    return LocalState("in library", folder, f"in the library at {folder}")
            except OSError:
                continue
        hit = self.cached.get(repo_id.lower())
        if hit is None:
            return LocalState()
        info, rev = hit
        try:
            entry = cache_entry(info, rev, self.dest, self.cache, self.layout)
        except OSError:
            return LocalState("unverified", info.repo_path, "in the hub cache")
        return LocalState(entry.state, entry.path, f"in the hub cache: {entry.note}")


class LibraryTab(QWidget):
    """The "Libraries" tab: every model folder in the destination, plus cache leftovers."""

    def __init__(self, window: "MainWindow") -> None:
        super().__init__()
        self.window = window
        self.scanner: LibraryScanner | None = None
        self.entries: list[LibraryEntry] = []

        root = QVBoxLayout(self)
        top = QHBoxLayout()
        self.where = QLineEdit()
        self.where.setPlaceholderText(str(hff.DEFAULT_DEST))
        self.where.setToolTip("The library folder. Type a path and press Enter (or leave the field) to switch to it.")
        self.where.editingFinished.connect(self._where_changed)
        self.where_browse_btn = QPushButton("Browse...")
        self.where_browse_btn.clicked.connect(self._browse_where)
        self.refresh_btn = QPushButton("Refresh")
        self.refresh_btn.clicked.connect(self.refresh)
        self.open_btn = QPushButton("Open library folder")
        self.open_btn.clicked.connect(lambda: self.window._open_folder(str(self.window._options().dest_path())))
        self.open_model_btn = QPushButton("Open model folder")
        self.open_model_btn.clicked.connect(self._open_selected)
        self.verify_btn = QPushButton("Verify")
        self.verify_btn.setToolTip("Check the selected cached model file by file against the Hub (changes nothing)")
        self.verify_btn.clicked.connect(self._verify_selected)
        self.resume_btn = QPushButton("Resume download")
        self.resume_btn.setToolTip("Fetch what is missing of the selected cached model, then move it to the library")
        self.resume_btn.clicked.connect(self._resume_selected)
        mark(self.resume_btn, "primary")
        top.addWidget(QLabel("Library:"))
        top.addWidget(self.where, 1)
        top.addWidget(self.where_browse_btn)
        top.addWidget(self.verify_btn)
        top.addWidget(self.resume_btn)
        top.addWidget(self.open_model_btn)
        top.addWidget(self.open_btn)
        top.addWidget(self.refresh_btn)
        root.addLayout(top)

        self.table = QTableWidget(0, 8)
        self.table.setHorizontalHeaderLabels(["Model", "State", "Action", "Files", "Size", "Last changed", "Details", "Folder"])
        header = self.table.horizontalHeader()
        header.setMinimumSectionSize(70)
        for col in range(6):
            header.setSectionResizeMode(col, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(6, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(7, QHeaderView.ResizeMode.Stretch)
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setAlternatingRowColors(True)
        self.table.setSortingEnabled(True)
        self.table.doubleClicked.connect(lambda _idx: self._open_selected())
        self.table.itemSelectionChanged.connect(self._selection_changed)
        root.addWidget(self.table, 1)

        self.summary = QLabel("Not scanned yet.")
        root.addWidget(self.summary)
        self._selection_changed()

    @Slot()
    def _where_changed(self) -> None:
        """Typed a new library folder here: it becomes the library folder everywhere."""
        text = self.where.text().strip()
        if text != self.window.dest_edit.text().strip():
            self.window.dest_edit.setText(text)
            self.window._dest_changed()

    def _browse_where(self) -> None:
        # start where the library is now (the last used folder); if that folder is not there
        # yet, start at its parent so the dialog opens somewhere sensible
        start = Path(self.where.text().strip() or str(self.window._options().dest_path()))
        while not start.is_dir() and start.parent != start:
            start = start.parent
        chosen = QFileDialog.getExistingDirectory(self, "Choose the library folder", str(start))
        if chosen:
            self.where.setText(os.path.normpath(chosen))
            self._where_changed()

    @Slot()
    def refresh(self) -> None:
        if self.scanner is not None:
            return
        opts = self.window._options()
        if not self.where.hasFocus():
            self.where.setText(opts.dest)  # empty means the default, shown as the placeholder
        self.refresh_btn.setEnabled(False)
        self.summary.setText("Scanning...")
        self.scanner = LibraryScanner(opts.dest_path(), opts.cache_path(), opts.layout, self)
        self.scanner.found.connect(self._show)
        self.scanner.finished.connect(self._scan_finished)
        self.scanner.start()

    @Slot()
    def _scan_finished(self) -> None:
        if self.scanner is not None:
            self.scanner.deleteLater()
        self.scanner = None
        self.refresh_btn.setEnabled(True)

    @Slot(list)
    def _show(self, entries: list) -> None:
        self.entries = entries
        self.table.setSortingEnabled(False)
        self.table.setRowCount(0)
        for e in entries:
            row = self.table.rowCount()
            self.table.insertRow(row)
            files = str(e.files)
            size = hff.human(e.size) if e.size else ""
            if e.state == "incomplete":
                files = f"{e.files} / {e.expected_files}"
                size = f"{hff.human(e.size)} / {hff.human(e.expected_size)}"
            cells = [
                e.name,
                LIBRARY_LABELS.get(e.state, e.state),
                "",
                files,
                size,
                time.strftime("%Y-%m-%d %H:%M", time.localtime(e.modified)) if e.modified else "",
                e.note,
                str(e.path),
            ]
            for col, text in enumerate(cells):
                item = QTableWidgetItem(text)
                if col == 0:
                    item.setData(Qt.ItemDataRole.UserRole, e)
                if col in (3, 4):
                    item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
                if col == 1:
                    color = LIBRARY_COLORS.get(e.state)
                    if color:
                        tint_item(item, QColor(color))
                self.table.setItem(row, col, item)
            action = self._action_button(e)
            if action is not None:
                self.table.setCellWidget(row, 2, action)
        self.table.setSortingEnabled(True)
        self._selection_changed()
        ready = [e for e in entries if e.state == "ready"]
        cached = [e for e in entries if e.in_cache]
        incomplete = [e for e in entries if e.state == "incomplete"]
        downloading = [e for e in entries if e.state == "downloading"]
        text = f"{len(ready)} finished model(s) in the library, {hff.human(sum(e.size for e in ready))}"
        if cached:
            text += f"; {len(cached)} in the hub cache"
            if downloading:
                text += f", {len(downloading)} being downloaded by another process"
            if incomplete:
                text += f", {len(incomplete)} not finished (click its Resume download button)"
        self.summary.setText(text)

    def _action_button(self, e: LibraryEntry) -> QPushButton | None:
        """The row's own button: resume a download that is not finished, move a finished one out of the cache."""
        if e.state in ("incomplete", "unverified"):
            btn = QPushButton("Resume download")
            btn.setToolTip("Fetch the missing files of this model, verify it and move it to the library")
            btn.clicked.connect(lambda _c=False, rid=e.repo_id: self._resume(rid))
            mark(btn, "primary")
        elif e.state == "in cache":
            btn = QPushButton("Move to library")
            btn.setToolTip("Verify this finished model and move it out of the cache into the library")
            btn.clicked.connect(lambda _c=False, rid=e.repo_id: self._resume(rid))
        elif e.state == "downloading":
            btn = QPushButton("Resume here")
            btn.setToolTip("Another process is downloading this; this would stop waiting for it and fetch what is missing here")
            btn.clicked.connect(lambda _c=False, rid=e.repo_id: self._resume(rid))
        elif e.state == "ready":
            btn = QPushButton("Open folder")
            btn.clicked.connect(lambda _c=False, p=str(e.path): self.window._open_folder(p))
        else:
            return None
        btn.setCursor(Qt.CursorShape.PointingHandCursor)
        return btn

    def _resume(self, repo_id: str) -> None:
        self.window.show_download_tab()
        self.window.start_resume(repo_id)

    def _selected(self) -> LibraryEntry | None:
        rows = self.table.selectionModel().selectedRows()
        if not rows:
            return None
        return self.table.item(rows[0].row(), 0).data(Qt.ItemDataRole.UserRole)

    @Slot()
    def _selection_changed(self) -> None:
        e = self._selected()
        self.open_model_btn.setEnabled(e is not None)
        self.verify_btn.setEnabled(e is not None and e.in_cache)
        self.resume_btn.setEnabled(e is not None and e.in_cache and e.state not in ("in cache", "downloading"))
        self.window.follow_selection(e.repo_id if e is not None and e.in_cache else None)

    def _open_selected(self) -> None:
        e = self._selected()
        if e is None:
            QMessageBox.information(self, APP_NAME, "Select a model first.")
            return
        self.window._open_folder(str(e.path))

    def _verify_selected(self) -> None:
        e = self._selected()
        if e is not None and e.in_cache:
            self.window.show_download_tab()
            self.window.start_verify(e.repo_id)

    def _resume_selected(self) -> None:
        e = self._selected()
        if e is not None and e.in_cache:
            self._resume(e.repo_id)


# internal state keys -> what the tables say; "finished" or not is the point
LIBRARY_LABELS = {
    "ready": "finished",
    "in cache": "finished, in cache",
    "incomplete": "not finished",
    "downloading": "downloading",
    "moving": "moving",
    "unverified": "unverified",
    "in library": "finished, in library",
}
LIBRARY_COLORS = {
    "ready": "#1e8e3e",
    "moving": "#b06000",
    "in cache": "#1a73e8",
    "incomplete": "#b06000",
    "downloading": "#b06000",
    "unverified": "#5f6368",
}


# --------------------------------------------------------------------------- hub browser

SORT_OPTIONS = [
    ("Trending", "trending_score"),
    ("Most downloads (30 days)", "downloads"),
    ("Most likes", "likes"),
    ("Recently updated", "last_modified"),
    ("Recently created", "created_at"),
]
TASKS = [
    "",
    "text-generation",
    "image-text-to-text",
    "text-to-image",
    "image-to-image",
    "text-to-video",
    "image-to-video",
    "text-to-speech",
    "automatic-speech-recognition",
    "audio-to-audio",
    "text-to-audio",
    "feature-extraction",
    "sentence-similarity",
    "fill-mask",
    "text-classification",
    "token-classification",
    "question-answering",
    "zero-shot-classification",
    "translation",
    "summarization",
    "image-classification",
    "object-detection",
    "image-segmentation",
    "depth-estimation",
    "any-to-any",
    "reinforcement-learning",
    "robotics",
]
EXPAND_FIELDS = [
    "author",
    "downloads",
    "downloadsAllTime",
    "likes",
    "trendingScore",
    "pipeline_tag",
    "library_name",
    "createdAt",
    "lastModified",
    "gated",
    "tags",
]
MAX_CARD_IMAGES = 24
MAX_IMAGE_BYTES = 15 << 20
MAX_IMAGE_WIDTH = 900

# model categories (families of pipeline tags) and their colours
ACCENT = "#ff9d00"  # Hugging Face yellow-orange, the window's accent
CATEGORY_COLORS = {
    "Multimodal": "#e8710a",
    "Text": "#1a73e8",
    "Image": "#9334e6",
    "Video": "#d01884",
    "Audio": "#1e8e3e",
    "Embeddings": "#12838f",
    "Agents": "#8a5a00",
    "Other": "#5f6368",
}
LOCAL_COLORS = {
    "in library": "#1e8e3e",
    "in cache": "#1a73e8",
    "incomplete": "#b06000",
    "downloading": "#b06000",
    "unverified": "#5f6368",
}


def task_category(task: str) -> str:
    t = (task or "").lower()
    if not t:
        return "Other"
    if t in ("any-to-any", "image-text-to-text", "image-to-text", "visual-question-answering", "document-question-answering", "video-text-to-text", "audio-text-to-text", "visual-document-retrieval"):
        return "Multimodal"
    if "video" in t:
        return "Video"
    if any(k in t for k in ("image", "depth", "object-detection", "segmentation", "mask-generation", "keypoint", "unconditional")):
        return "Image"
    if any(k in t for k in ("audio", "speech", "voice")):
        return "Audio"
    if any(k in t for k in ("feature-extraction", "sentence-similarity", "embedding", "reranking")):
        return "Embeddings"
    if any(k in t for k in ("robotics", "reinforcement")):
        return "Agents"
    if any(k in t for k in ("text", "question-answering", "translation", "summarization", "fill-mask", "token-classification", "zero-shot", "table", "conversational")):
        return "Text"
    return "Other"


def category_color(task: str) -> QColor:
    return QColor(CATEGORY_COLORS[task_category(task)])


def contrast_text(bg: QColor) -> QColor:
    """Text colour for a coloured box: the opposite hue, pushed to the opposite lightness.

    Dark boxes get a pale complementary tint, light boxes a deep one, so the text
    is both the colour's opposite and readable.
    """
    r, g, b = bg.redF(), bg.greenF(), bg.blueF()

    def lin(c: float) -> float:
        return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4

    def luminance(c: QColor) -> float:
        return 0.2126 * lin(c.redF()) + 0.7152 * lin(c.greenF()) + 0.0722 * lin(c.blueF())

    def ratio(a: QColor, b: QColor) -> float:
        la, lb = luminance(a), luminance(b)
        return (max(la, lb) + 0.05) / (min(la, lb) + 0.05)

    hue = bg.hslHueF()
    hue = 0.0 if hue < 0 else (hue + 0.5) % 1.0  # complementary; greys have no hue
    saturation = 0.0 if bg.hslSaturationF() < 0.08 else 0.45
    pale = QColor.fromHslF(hue, saturation, 0.96)
    deep = QColor.fromHslF(hue, saturation, 0.07)
    # the opposite lightness first; fall back to the other side, then to white/black,
    # so the text always clears the 4.5:1 contrast that body text needs
    order = [pale, deep] if luminance(bg) < 0.3 else [deep, pale]
    order += [QColor("#ffffff"), QColor("#000000")]
    for candidate in order:
        if ratio(bg, candidate) >= 4.5:
            return candidate
    return max(order, key=lambda c: ratio(bg, c))


def tint_item(item: QTableWidgetItem, color: QColor) -> None:
    """A solid coloured box with the text in the colour's opposite."""
    item.setBackground(color)
    item.setForeground(contrast_text(color))


def chip_style(color: str) -> str:
    """Stylesheet for a small coloured label with opposite-coloured text."""
    return f"background: {color}; color: {contrast_text(QColor(color)).name()}; padding: 1px 7px; border-radius: 3px;"


@dataclass
class HubModel:
    repo_id: str
    author: str = ""
    task: str = ""
    library: str = ""
    downloads: int = 0
    downloads_all: int = 0
    likes: int = 0
    trending: float = 0.0
    updated: datetime | None = None
    created: datetime | None = None
    storage: int = 0
    gated: str = ""
    tags: list[str] = field(default_factory=list)
    local: LocalState = field(default_factory=LocalState)

    @property
    def category(self) -> str:
        return task_category(self.task)

    @classmethod
    def from_info(cls, info) -> "HubModel":
        gated = getattr(info, "gated", None)
        return cls(
            repo_id=info.id,
            author=getattr(info, "author", None) or info.id.split("/")[0],
            task=getattr(info, "pipeline_tag", None) or "",
            library=getattr(info, "library_name", None) or "",
            downloads=int(getattr(info, "downloads", None) or 0),
            downloads_all=int(getattr(info, "downloads_all_time", None) or 0),
            likes=int(getattr(info, "likes", None) or 0),
            trending=float(getattr(info, "trending_score", None) or 0.0),
            updated=getattr(info, "last_modified", None),
            created=getattr(info, "created_at", None),
            storage=int(getattr(info, "used_storage", None) or 0),
            gated="" if not gated else ("yes" if gated is True else str(gated)),
            tags=[t for t in (getattr(info, "tags", None) or []) if ":" not in t or t.startswith("base_model:")],
        )

    @property
    def url(self) -> str:
        return f"{HUB_URL}/{self.repo_id}"


def fmt_count(n: int | float) -> str:
    n = float(n)
    for unit, div in (("B", 1e9), ("M", 1e6), ("k", 1e3)):
        if abs(n) >= div:
            v = n / div
            return f"{v:.1f}{unit}" if v < 10 else f"{v:.0f}{unit}"
    return f"{n:.0f}"


def fmt_date(d: datetime | None) -> str:
    return d.strftime("%Y-%m-%d") if d else ""


class BrowseWorker(QThread):
    """Pulls one page of models out of a list_models iterator (which paginates lazily)
    and looks each one up on disk."""

    page = Signal(list, bool)  # models, more may follow
    failed = Signal(str)

    def __init__(self, iterator, page_size: int, opts: Options, parent=None) -> None:
        super().__init__(parent)
        self.iterator = iterator
        self.page_size = page_size
        self.opts = opts

    def run(self) -> None:
        rows: list[HubModel] = []
        try:
            for info in self.iterator:
                rows.append(HubModel.from_info(info))
                if len(rows) >= self.page_size:
                    break
        except Exception as exc:  # noqa: BLE001 - network, auth, bad filter
            self.failed.emit(f"{type(exc).__name__}: {exc}")
            return
        index = LocalIndex(self.opts.dest_path(), self.opts.cache_path(), self.opts.layout)
        for m in rows:
            m.local = index.lookup(m.repo_id)
        self.page.emit(rows, len(rows) >= self.page_size)


class LocalStateWorker(QThread):
    """Re-checks which of the listed models are on disk (after a download or a move)."""

    states = Signal(dict)  # repo id -> LocalState

    def __init__(self, repo_ids: list[str], opts: Options, parent=None) -> None:
        super().__init__(parent)
        self.repo_ids = repo_ids
        self.opts = opts

    def run(self) -> None:
        index = LocalIndex(self.opts.dest_path(), self.opts.cache_path(), self.opts.layout)
        self.states.emit({repo_id: index.lookup(repo_id) for repo_id in self.repo_ids})


class NumItem(QTableWidgetItem):
    """Table cell that shows a formatted value but sorts by the raw number or date."""

    def __init__(self, text: str, key) -> None:
        super().__init__(text)
        self.key = key
        self.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)

    def __lt__(self, other) -> bool:  # type: ignore[override]
        if isinstance(other, NumItem):
            a, b = self.key, other.key
            if a is None:
                return b is not None
            if b is None:
                return False
            return a < b
        return super().__lt__(other)


BROWSE_COLUMNS = [
    "Model",
    "Downloaded",
    "Author",
    "Task",
    "Library",
    "Downloads (30d)",
    "All time",
    "Likes",
    "Trending",
    "Updated",
    "Created",
    "Gated",
    "Tags",
]


class BrowseTab(QWidget):
    """The "Browse Hub" tab: the Hub's model listing, one page at a time, sortable by every column."""

    def __init__(self, window: "MainWindow") -> None:
        super().__init__()
        self.window = window
        self.worker: BrowseWorker | None = None
        self.local_worker: LocalStateWorker | None = None
        self.iterator = None
        self.page_no = 0
        self.models: list[HubModel] = []
        self.rows: dict[str, int] = {}  # repo id -> table row (the table is re-sorted, so looked up by item)

        root = QVBoxLayout(self)
        form = QFormLayout()
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.ExpandingFieldsGrow)
        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText("words in the repo id, e.g. llama 3 instruct gguf")
        self.search_edit.returnPressed.connect(self.scrape_first)
        form.addRow("Search:", self.search_edit)

        filters = QHBoxLayout()
        self.author_edit = QLineEdit()
        self.author_edit.setPlaceholderText("org or user")
        self.author_edit.returnPressed.connect(self.scrape_first)
        self.task_combo = QComboBox()
        self.task_combo.setEditable(True)
        self.task_combo.addItems(TASKS)
        self.task_combo.setToolTip("pipeline tag; type any other task name")
        self.tag_edit = QLineEdit()
        self.tag_edit.setPlaceholderText("tags, e.g. gguf safetensors en")
        self.tag_edit.setToolTip("Only models carrying all of these tags (library names like transformers or gguf are tags too)")
        self.tag_edit.returnPressed.connect(self.scrape_first)
        filters.addWidget(QLabel("Author:"))
        filters.addWidget(self.author_edit, 1)
        filters.addWidget(QLabel("Task:"))
        filters.addWidget(self.task_combo, 1)
        filters.addWidget(QLabel("Tags:"))
        filters.addWidget(self.tag_edit, 1)
        form.addRow("Filter:", filters)

        order = QHBoxLayout()
        self.sort_combo = QComboBox()
        for label, _key in SORT_OPTIONS:
            self.sort_combo.addItem(label)
        self.sort_combo.setToolTip("Order the Hub returns pages in; click any column header to re-sort what is listed")
        self.page_spin = QSpinBox()
        self.page_spin.setRange(10, 100)
        self.page_spin.setValue(30)
        self.page_spin.setSuffix(" per page")
        self.scrape_btn = QPushButton("Scrape page 1")
        self.scrape_btn.setToolTip("Fetch the first page of the listing with these filters")
        self.scrape_btn.clicked.connect(self.scrape_first)
        mark(self.scrape_btn, "primary")
        self.next_btn = QPushButton("Next page")
        self.next_btn.setToolTip("Append the next page to the list")
        self.next_btn.clicked.connect(self.scrape_next)
        order.addWidget(QLabel("Sort by:"))
        order.addWidget(self.sort_combo)
        order.addWidget(self.page_spin)
        order.addStretch(1)
        order.addWidget(self.scrape_btn)
        order.addWidget(self.next_btn)
        form.addRow("Pages:", order)
        root.addLayout(form)

        # clickable legend: a chip filters the list to that category / download state and sorts by it
        self.category_filter = ""
        self.local_filter = ""
        self.category_chips: dict[str, QPushButton] = {}
        self.local_chips: dict[str, QPushButton] = {}
        legend = QHBoxLayout()
        legend.addWidget(QLabel("Categories:"))
        for name, color in CATEGORY_COLORS.items():
            chip = self._chip(name, color, f"Show only {name} models, sorted by task; click again for all")
            chip.clicked.connect(lambda _c=False, n=name: self._toggle_category(n))
            self.category_chips[name] = chip
            legend.addWidget(chip)
        legend.addSpacing(24)
        legend.addWidget(QLabel("Downloaded:"))
        for name, color in LOCAL_COLORS.items():
            if name == "unverified":
                continue
            chip = self._chip(LIBRARY_LABELS.get(name, name), color, f"Show only models that are {LIBRARY_LABELS.get(name, name)}; click again for all")
            chip.clicked.connect(lambda _c=False, n=name: self._toggle_local(n))
            self.local_chips[name] = chip
            legend.addWidget(chip)
        self.filter_label = QLabel("")
        legend.addSpacing(12)
        legend.addWidget(self.filter_label)
        legend.addStretch(1)
        root.addLayout(legend)

        self.table = QTableWidget(0, len(BROWSE_COLUMNS))
        self.table.setHorizontalHeaderLabels(BROWSE_COLUMNS)
        header = self.table.horizontalHeader()
        header.setMinimumSectionSize(60)
        for col in range(len(BROWSE_COLUMNS) - 1):
            header.setSectionResizeMode(col, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(len(BROWSE_COLUMNS) - 1, QHeaderView.ResizeMode.Stretch)
        header.setSortIndicatorShown(True)
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)  # Ctrl/Shift-click for several
        self.table.setAlternatingRowColors(True)
        self.table.setSortingEnabled(True)
        self.table.doubleClicked.connect(lambda _idx: self._details())
        self.table.itemSelectionChanged.connect(self._selection_changed)
        root.addWidget(self.table, 1)

        actions = QHBoxLayout()
        self.status = QLabel("Nothing scraped yet.")
        self.selection_label = QLabel("")
        self.selection_label.setToolTip("Ctrl-click or Shift-click rows to select several models; Ctrl+A selects the whole page")
        self.details_btn = QPushButton("View details")
        self.details_btn.setToolTip("The whole model card with its images, plus the file list (first selected model)")
        self.details_btn.clicked.connect(self._details)
        self.download_btn = QPushButton("Download")
        self.download_btn.setToolTip("Download every selected model (several run at once, the rest queue up)")
        self.download_btn.clicked.connect(self._download)
        mark(self.download_btn, "primary")
        self.add_btn = QPushButton("Add to list")
        self.add_btn.setToolTip("Append every selected model to the list on the Download tab")
        self.add_btn.clicked.connect(self._add)
        self.open_btn = QPushButton("Open on huggingface.co")
        self.open_btn.clicked.connect(self._open)
        actions.addWidget(self.status, 1)
        actions.addWidget(self.selection_label)
        actions.addWidget(self.details_btn)
        actions.addWidget(self.download_btn)
        actions.addWidget(self.add_btn)
        actions.addWidget(self.open_btn)
        root.addLayout(actions)
        self._selection_changed()
        self.next_btn.setEnabled(False)

    # -- legend chips: filter + sort

    @staticmethod
    def _chip(text: str, color: str, tip: str) -> QPushButton:
        chip = QPushButton(text)
        chip.setCheckable(True)
        chip.setCursor(Qt.CursorShape.PointingHandCursor)
        chip.setToolTip(tip)
        chip.setStyleSheet(
            f"QPushButton {{ {chip_style(color)} border: 2px solid transparent; }}"
            f"QPushButton:checked {{ border: 2px solid palette(text); font-weight: bold; }}"
        )
        return chip

    def _toggle_category(self, name: str) -> None:
        self.category_filter = "" if self.category_filter == name else name
        self._apply_filters(sort_by_task=bool(self.category_filter))

    def _toggle_local(self, name: str) -> None:
        self.local_filter = "" if self.local_filter == name else name
        self._apply_filters(sort_by_task=False)

    def _apply_filters(self, sort_by_task: bool = False) -> None:
        for name, chip in self.category_chips.items():
            chip.setChecked(name == self.category_filter)
        for name, chip in self.local_chips.items():
            chip.setChecked(name == self.local_filter)
        shown = 0
        for row in range(self.table.rowCount()):
            m = self.table.item(row, 0).data(Qt.ItemDataRole.UserRole)
            hide = (self.category_filter and m.category != self.category_filter) or (self.local_filter and m.local.state != self.local_filter)
            self.table.setRowHidden(row, bool(hide))
            shown += 0 if hide else 1
        if sort_by_task:
            self.table.sortItems(3, Qt.SortOrder.AscendingOrder)  # task column: models of one kind together
        if self.category_filter or self.local_filter:
            what = " and ".join(x for x in (self.category_filter, LIBRARY_LABELS.get(self.local_filter, self.local_filter) if self.local_filter else "") if x)
            self.filter_label.setText(f"showing {shown} of {self.table.rowCount()} ({what})")
        else:
            self.filter_label.setText("")
        self._selection_changed()

    # -- scraping

    def _make_iterator(self):
        sort_key = SORT_OPTIONS[self.sort_combo.currentIndex()][1]
        tags = [t for t in self.tag_edit.text().replace(",", " ").split() if t]
        task = self.task_combo.currentText().strip()
        api = hff.HfApi()
        return api.list_models(
            search=self.search_edit.text().strip() or None,
            author=self.author_edit.text().strip() or None,
            pipeline_tag=task or None,
            filter=tags or None,
            sort=sort_key,
            limit=None,
            expand=EXPAND_FIELDS,
        )

    @Slot()
    def scrape_first(self) -> None:
        if self.worker is not None:
            return
        self.iterator = self._make_iterator()
        self.page_no = 0
        self.models = []
        self.table.setSortingEnabled(False)
        self.table.setRowCount(0)
        self.table.setSortingEnabled(True)
        self._fetch_page()

    @Slot()
    def scrape_next(self) -> None:
        if self.worker is not None or self.iterator is None:
            return
        self._fetch_page()

    def _fetch_page(self) -> None:
        self.status.setText(f"Fetching page {self.page_no + 1}...")
        self.scrape_btn.setEnabled(False)
        self.next_btn.setEnabled(False)
        self.worker = BrowseWorker(self.iterator, self.page_spin.value(), self.window._options(), self)
        self.worker.page.connect(self._page)
        self.worker.failed.connect(self._failed)
        self.worker.finished.connect(self._worker_finished)
        self.worker.start()

    @Slot()
    def _worker_finished(self) -> None:
        if self.worker is not None:
            self.worker.deleteLater()
        self.worker = None
        self.scrape_btn.setEnabled(True)

    @Slot(str)
    def _failed(self, msg: str) -> None:
        self.status.setText("Could not fetch the listing: " + msg)
        self.iterator = None

    @Slot(list, bool)
    def _page(self, rows: list, more: bool) -> None:
        self.page_no += 1
        self.models.extend(rows)
        self.table.setSortingEnabled(False)
        for m in rows:
            row = self.table.rowCount()
            self.table.insertRow(row)
            local = QTableWidgetItem(m.local.state)
            self._paint_local(local, m.local)
            task = QTableWidgetItem(m.task)
            task.setToolTip(f"{m.category} model")
            if m.task:
                tint_item(task, category_color(m.task))
            cells: list[QTableWidgetItem] = [
                QTableWidgetItem(m.repo_id),
                local,
                QTableWidgetItem(m.author),
                task,
                QTableWidgetItem(m.library),
                NumItem(fmt_count(m.downloads), m.downloads),
                NumItem(fmt_count(m.downloads_all), m.downloads_all),
                NumItem(fmt_count(m.likes), m.likes),
                NumItem(f"{m.trending:.0f}", m.trending),
                NumItem(fmt_date(m.updated), m.updated.timestamp() if m.updated else None),
                NumItem(fmt_date(m.created), m.created.timestamp() if m.created else None),
                QTableWidgetItem(m.gated),
                QTableWidgetItem(", ".join(m.tags)),
            ]
            cells[0].setData(Qt.ItemDataRole.UserRole, m)
            cells[0].setToolTip(m.url)
            cells[-1].setToolTip("\n".join(m.tags))
            for col, item in enumerate(cells):
                self.table.setItem(row, col, item)
        self.table.setSortingEnabled(True)
        self.next_btn.setEnabled(more)
        on_disk = sum(1 for m in self.models if m.local.state)
        text = f"Page {self.page_no}: {len(self.models)} model(s) listed"
        if on_disk:
            text += f", {on_disk} already on this machine"
        if not rows:
            text = f"No more models after page {self.page_no - 1} ({len(self.models)} listed)" if self.models else "No model matches these filters."
        elif not more:
            text += ", that is all of them"
        self.status.setText(text)
        self._apply_filters(sort_by_task=bool(self.category_filter))

    @staticmethod
    def _paint_local(item: QTableWidgetItem, local: LocalState) -> None:
        item.setText(LIBRARY_LABELS.get(local.state, local.state))
        item.setToolTip(local.note)
        color = LOCAL_COLORS.get(local.state)
        if color:
            tint_item(item, QColor(color))

    def refresh_local(self) -> None:
        """Re-check the Downloaded column after a job (download, move) changed what is on disk."""
        if not self.models or self.local_worker is not None:
            return
        self.local_worker = LocalStateWorker([m.repo_id for m in self.models], self.window._options(), self)
        self.local_worker.states.connect(self._local_states)
        self.local_worker.finished.connect(self._local_finished)
        self.local_worker.start()

    @Slot()
    def _local_finished(self) -> None:
        if self.local_worker is not None:
            self.local_worker.deleteLater()
        self.local_worker = None

    @Slot(dict)
    def _local_states(self, states: dict) -> None:
        for row in range(self.table.rowCount()):
            m = self.table.item(row, 0).data(Qt.ItemDataRole.UserRole)
            local = states.get(m.repo_id)
            if local is None:
                continue
            m.local = local
            self._paint_local(self.table.item(row, 1), local)
        if self.local_filter:
            self._apply_filters()

    # -- selection

    def selected_models(self) -> list[HubModel]:
        rows = sorted(idx.row() for idx in self.table.selectionModel().selectedRows() if not self.table.isRowHidden(idx.row()))
        return [self.table.item(r, 0).data(Qt.ItemDataRole.UserRole) for r in rows]

    def selected(self) -> HubModel | None:
        models = self.selected_models()
        return models[0] if models else None

    @Slot()
    def _selection_changed(self) -> None:
        models = self.selected_models()
        n = len(models)
        for btn in (self.details_btn, self.download_btn, self.add_btn, self.open_btn):
            btn.setEnabled(n > 0)
        self.download_btn.setText(f"Download {n} models" if n > 1 else "Download")
        self.add_btn.setText(f"Add {n} to list" if n > 1 else "Add to list")
        self.selection_label.setText(f"{n} selected" if n > 1 else "")
        self.window.follow_selection(models[0].repo_id if models else None)

    def _details(self) -> None:
        m = self.selected()
        if m is not None:
            self.window.show_details(m.repo_id)

    def _download(self) -> None:
        models = self.selected_models()
        if len(models) == 1:
            self.window.download_repo(models[0].repo_id)
        elif models:
            self.window.download_many([(m.repo_id, "") for m in models])

    def _add(self) -> None:
        for m in self.selected_models():
            self.window.add_to_list(m.repo_id)

    def _open(self) -> None:
        models = self.selected_models()
        for m in models[:6]:  # a tab per model, within reason
            QDesktopServices.openUrl(QUrl(m.url))
        if len(models) > 6:
            self.window.statusBar().showMessage(f"opened the first 6 of {len(models)} selected models", 5000)


# --------------------------------------------------------------------------- model card


@dataclass
class ModelCard:
    repo_id: str
    model: HubModel | None = None
    meta: dict[str, str] = field(default_factory=dict)  # front matter, flattened
    markdown: str = ""  # the card body with absolute links
    images: dict[str, QImage] = field(default_factory=dict)  # url -> image
    files: list[tuple[str, int]] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


def fetch_url(url: str, limit: int = MAX_IMAGE_BYTES, timeout: int = 30) -> bytes:
    headers = {"User-Agent": f"{APP_NAME}/{APP_VERSION}"}
    if url.startswith(HUB_URL + "/"):
        token = None
        try:
            from huggingface_hub import get_token

            token = get_token()
        except Exception:  # noqa: BLE001 - no token, public access only
            pass
        if token:
            headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read(limit)


def split_front_matter(text: str) -> tuple[dict[str, str], str]:
    """Split the YAML header off a model card; the header is flattened to key -> text."""
    if not text.startswith("---"):
        return {}, text
    lines = text.splitlines()
    end = next((i for i in range(1, len(lines)) if lines[i].strip() == "---"), None)
    if end is None:
        return {}, text
    meta: dict[str, str] = {}
    key = ""
    for line in lines[1:end]:
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if not line.startswith((" ", "\t", "-")) and ":" in line:
            key, _, value = line.partition(":")
            key = key.strip()
            meta[key] = value.strip().strip("\"'")
        elif key and line.strip().startswith("- "):
            item = line.strip()[2:].strip().strip("\"'")
            meta[key] = (meta[key] + ", " + item) if meta[key] else item
    return meta, "\n".join(lines[end + 1 :])


MD_LINK_RE = re.compile(r"(!?)\[([^\]]*)\]\(\s*<?([^)\s>]+)>?(\s+\"[^\"]*\")?\s*\)")
HTML_SRC_RE = re.compile(r"(<(?:img|source|video)\b[^>]*?\b(?:src|poster)\s*=\s*)([\"'])([^\"']+)\2", re.IGNORECASE)
HTML_HREF_RE = re.compile(r"(<a\b[^>]*?\bhref\s*=\s*)([\"'])([^\"']+)\2", re.IGNORECASE)


def absolutise(markdown: str, repo_id: str) -> tuple[str, list[str]]:
    """Make every relative link absolute (files resolve on the Hub) and list the image urls."""
    resolve = f"{HUB_URL}/{repo_id}/resolve/main/"
    blob = f"{HUB_URL}/{repo_id}/blob/main/"
    images: list[str] = []

    def fix(url: str, is_image: bool) -> str:
        if url.startswith(("#", "mailto:", "data:")):
            return url
        if "://" in url[:10]:
            return url
        if url.startswith("//"):
            return "https:" + url
        return urllib.parse.urljoin(resolve if is_image else blob, url)

    def md(m: re.Match) -> str:
        bang, text, url, title = m.group(1), m.group(2), m.group(3), m.group(4) or ""
        new = fix(url, bool(bang))
        if bang:
            images.append(new)
        return f"{bang}[{text}]({new}{title})"

    def src(m: re.Match) -> str:
        new = fix(m.group(3), True)
        images.append(new)
        return f"{m.group(1)}{m.group(2)}{new}{m.group(2)}"

    def href(m: re.Match) -> str:
        return f"{m.group(1)}{m.group(2)}{fix(m.group(3), False)}{m.group(2)}"

    out = MD_LINK_RE.sub(md, markdown)
    out = HTML_SRC_RE.sub(src, out)
    out = HTML_HREF_RE.sub(href, out)
    seen: set[str] = set()
    unique = [u for u in images if not (u in seen or seen.add(u))]
    return out, unique


DETAILS_RE = re.compile(r"</?details\b[^>]*>", re.IGNORECASE)
SUMMARY_OPEN_RE = re.compile(r"<summary\b[^>]*>", re.IGNORECASE)
SUMMARY_CLOSE_RE = re.compile(r"</summary\s*>", re.IGNORECASE)
STRIP_BLOCK_RE = re.compile(r"<(style|script)\b.*?</\1\s*>", re.IGNORECASE | re.DOTALL)
VIDEO_RE = re.compile(r"<video\b.*?</video\s*>", re.IGNORECASE | re.DOTALL)
SRC_ATTR_RE = re.compile(r"""src\s*=\s*["']([^"']+)["']""", re.IGNORECASE)


def render_card_html(md_text: str) -> str:
    """Model card Markdown -> HTML for QTextBrowser; "" when the markdown package is missing.

    Collapsed <details> blocks are opened (their text would otherwise be lost),
    <style>/<script> go, and <video> tags become links (Qt cannot play them).
    """
    text = STRIP_BLOCK_RE.sub("", md_text)
    text = DETAILS_RE.sub("", text)
    text = SUMMARY_OPEN_RE.sub("<p><b>", text)
    text = SUMMARY_CLOSE_RE.sub("</b></p>", text)

    def video(m: re.Match) -> str:
        links = [f'<a href="{s}">[video: {s.rsplit("/", 1)[-1]}]</a>' for s in SRC_ATTR_RE.findall(m.group(0))]
        return "<p>" + " ".join(links) + "</p>" if links else ""

    text = VIDEO_RE.sub(video, text)
    if _markdown is None:
        return ""
    try:
        return _markdown.markdown(
            text,
            extensions=["tables", "fenced_code", "sane_lists", "md_in_html", "attr_list"],
            output_format="html",
        )
    except Exception:  # noqa: BLE001 - an odd card must not break the window
        return ""


def is_dark(palette: QPalette) -> bool:
    return palette.window().color().lightnessF() < 0.5


def link_color(palette: QPalette) -> str:
    return "#6cb4ff" if is_dark(palette) else "#0b57d0"


def card_stylesheet(palette: QPalette) -> str:
    """CSS for the card document: readable links in both themes, visible table borders, code blocks."""
    dark = is_dark(palette)
    code_bg = "#2a2d31" if dark else "#f1f3f4"
    border = "#5a5f66" if dark else "#c9ccd1"
    quiet = "#9aa0a6" if dark else "#5f6368"
    return (
        f"a {{ color: {link_color(palette)}; text-decoration: underline; }}"
        f"code {{ background-color: {code_bg}; }}"
        f"pre {{ background-color: {code_bg}; padding: 6px; }}"
        f"table {{ border-collapse: collapse; }}"
        f"td, th {{ border: 1px solid {border}; padding: 4px 8px; }}"
        f"th {{ background-color: {code_bg}; font-weight: bold; }}"
        f"blockquote {{ color: {quiet}; }}"
        f"h1, h2, h3, h4 {{ color: {ACCENT}; }}"
    )


class CardWorker(QThread):
    """Fetches a model's info, file list, card text and the images the card shows."""

    ready = Signal(object)

    def __init__(self, repo_id: str, parent=None) -> None:
        super().__init__(parent)
        self.repo_id = repo_id

    def run(self) -> None:
        card = ModelCard(self.repo_id)
        try:
            info = hff.HfApi().model_info(self.repo_id, files_metadata=True)
            card.model = HubModel.from_info(info)
            card.files = sorted(
                ((s.rfilename, int(s.size or 0)) for s in (info.siblings or [])), key=lambda t: t[0].lower()
            )
        except Exception as exc:  # noqa: BLE001
            card.errors.append(f"model info: {type(exc).__name__}: {exc}")
        try:
            raw = fetch_url(f"{HUB_URL}/{self.repo_id}/resolve/main/README.md").decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            raw = ""
            if exc.code == 404:
                card.errors.append("this model has no model card (README.md)")
            elif exc.code in (401, 403):
                card.errors.append("the model card is gated or private; log in with `hf auth login` to read it")
            else:
                card.errors.append(f"model card: HTTP {exc.code}")
        except Exception as exc:  # noqa: BLE001
            raw = ""
            card.errors.append(f"model card: {type(exc).__name__}: {exc}")
        card.meta, body = split_front_matter(raw)
        card.markdown, urls = absolutise(body, self.repo_id)
        for url in urls[:MAX_CARD_IMAGES]:
            try:
                data = fetch_url(url, timeout=20)
            except Exception:  # noqa: BLE001 - a missing picture is not worth a message
                continue
            img = QImage.fromData(data)
            if img.isNull():
                continue
            if img.width() > MAX_IMAGE_WIDTH:
                img = img.scaledToWidth(MAX_IMAGE_WIDTH, Qt.TransformationMode.SmoothTransformation)
            card.images[url] = img
        self.ready.emit(card)


class CardView(QTextBrowser):
    """Renders a model card; images come from the dict the worker filled, not from the network."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.images: dict[str, QImage] = {}
        self.setOpenExternalLinks(True)
        self.setOpenLinks(True)

    def set_card(self, md_text: str) -> None:
        """Render Markdown with the theme's stylesheet (links readable on dark and light)."""
        self.document().setDefaultStyleSheet(card_stylesheet(self.palette()))
        html = render_card_html(md_text) if md_text.strip() else ""
        if html:
            self.setHtml(html)
        else:
            self.setMarkdown(md_text)

    def loadResource(self, kind: int, name: QUrl):  # noqa: N802 - Qt API
        img = self.images.get(name.toString())
        if img is not None:
            return img
        if name.scheme() in ("http", "https"):
            return QImage()  # never block the window on the network
        return super().loadResource(kind, name)


class ModelCardDialog(QDialog):
    """A window with the whole model card (text and images), stats and the file list."""

    def __init__(self, window: "MainWindow") -> None:
        super().__init__(window)
        self.window = window
        self.worker: CardWorker | None = None
        self.repo_id = ""
        self.setWindowTitle("Model card")
        self.setWindowFlag(Qt.WindowType.Window, True)
        self.resize(980, 760)

        root = QVBoxLayout(self)
        self.title = QLabel()
        font = QFont(self.title.font())
        font.setPointSize(font.pointSize() + 4)
        font.setBold(True)
        self.title.setFont(font)
        self.title.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.stats = QLabel()
        self.stats.setWordWrap(True)
        self.stats.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.meta = QLabel()
        self.meta.setWordWrap(True)
        self.meta.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        root.addWidget(self.title)
        root.addWidget(self.stats)
        root.addWidget(self.meta)

        buttons = QHBoxLayout()
        self.download_btn = QPushButton("Download")
        self.download_btn.clicked.connect(lambda: self.window.download_repo(self.repo_id))
        mark(self.download_btn, "primary")
        self.add_btn = QPushButton("Add to list")
        self.add_btn.clicked.connect(lambda: self.window.add_to_list(self.repo_id))
        self.open_btn = QPushButton("Open on huggingface.co")
        self.open_btn.clicked.connect(lambda: QDesktopServices.openUrl(QUrl(f"{HUB_URL}/{self.repo_id}")))
        close_btn = QPushButton("Close")
        close_btn.clicked.connect(self.close)
        buttons.addWidget(self.download_btn)
        buttons.addWidget(self.add_btn)
        buttons.addWidget(self.open_btn)
        buttons.addStretch(1)
        buttons.addWidget(close_btn)
        root.addLayout(buttons)

        files_box = QWidget()
        files_layout = QVBoxLayout(files_box)
        files_layout.setContentsMargins(0, 0, 0, 0)
        self.files = QTableWidget(0, 2)
        self.files.setHorizontalHeaderLabels(["File", "Size"])
        fh = self.files.horizontalHeader()
        fh.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        fh.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        self.files.verticalHeader().setVisible(False)
        self.files.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.files.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.files.setAlternatingRowColors(True)
        self.files.setSortingEnabled(True)
        self.files.doubleClicked.connect(lambda _idx: self._view_file())
        self.files.itemSelectionChanged.connect(self._file_selection_changed)
        file_btns = QHBoxLayout()
        self.file_hint = QLabel("Double-click a .json, .md or other text file to read it")
        self.file_hint.setEnabled(False)
        self.view_file_btn = QPushButton("View file")
        self.view_file_btn.setToolTip("Show this file: JSON pretty-printed, Markdown rendered, other text as is")
        self.view_file_btn.clicked.connect(self._view_file)
        self.view_file_btn.setEnabled(False)
        file_btns.addWidget(self.file_hint, 1)
        file_btns.addWidget(self.view_file_btn)
        files_layout.addWidget(self.files, 1)
        files_layout.addLayout(file_btns)
        self.file_dialog: FileViewerDialog | None = None
        self.card_commit = ""
        self.view = CardView()
        split = QSplitter(Qt.Orientation.Vertical)
        split.addWidget(files_box)
        split.addWidget(self.view)
        split.setStretchFactor(0, 1)
        split.setStretchFactor(1, 4)
        root.addWidget(split, 1)

    def load(self, repo_id: str, raise_window: bool = True) -> None:
        """Show this model's card. With raise_window=False the window just follows along
        (the user is selecting rows elsewhere and keeps the focus there)."""
        if repo_id == self.repo_id and self.worker is not None:
            return  # already loading it
        self.repo_id = repo_id
        self.setWindowTitle(f"{repo_id} - model card")
        self.title.setText(repo_id)
        self.stats.setText("Loading...")
        self.meta.setText("")
        self.files.setRowCount(0)
        self.view.images = {}
        self.view.setMarkdown("")
        if self.worker is not None:
            self.worker.ready.disconnect(self._ready)
        self.worker = CardWorker(repo_id, self)
        self.worker.ready.connect(self._ready)
        self.worker.finished.connect(self.worker.deleteLater)
        self.worker.start()
        self.show()
        if raise_window:
            self.raise_()
            self.activateWindow()

    @Slot(object)
    def _ready(self, card: ModelCard) -> None:
        self.worker = None
        if card.repo_id != self.repo_id:
            return
        m = card.model
        parts: list[str] = []
        if m is not None:
            parts.append(f"{fmt_count(m.downloads)} downloads last month, {fmt_count(m.downloads_all)} all time, {fmt_count(m.likes)} likes")
            if m.task:
                parts.append(f"task <b style='color:{category_color(m.task).name()}'>{m.task}</b> ({m.category})")
            if m.library:
                parts.append("library " + m.library)
            if m.updated:
                parts.append("updated " + fmt_date(m.updated))
            if m.storage:
                parts.append(hff.human(m.storage))
            if m.gated:
                parts.append("gated: " + m.gated)
        if card.errors:
            parts.extend(card.errors)
        local = self.window.local_state(card.repo_id)
        if local.state:
            color = LOCAL_COLORS.get(local.state, "#5f6368")
            parts.append(f"<b style='color:{color}'>{LIBRARY_LABELS.get(local.state, local.state)}</b>: {local.note}")
        self.stats.setText("; ".join(parts))
        keys = ("license", "base_model", "pipeline_tag", "language", "tags", "datasets", "library_name", "quantized_by")
        meta_bits = [f"<b>{k}</b>: {card.meta[k][:200]}" for k in keys if card.meta.get(k)]
        self.meta.setText("&nbsp;&nbsp;".join(meta_bits))
        self.files.setSortingEnabled(False)
        total = 0
        for name, size in card.files:
            row = self.files.rowCount()
            self.files.insertRow(row)
            self.files.setItem(row, 0, QTableWidgetItem(name))
            self.files.setItem(row, 1, NumItem(hff.human(size) if size else "", size))
            total += size
        self.files.setSortingEnabled(True)
        self.files.horizontalHeaderItem(1).setText(f"Size ({hff.human(total)} in {len(card.files)} files)" if card.files else "Size")
        self.view.images = card.images
        self.view.set_card(card.markdown or "*No model card text.*")
        self._file_selection_changed()

    # -- single files

    def _selected_file(self) -> tuple[str, int] | None:
        rows = self.files.selectionModel().selectedRows()
        if not rows:
            return None
        row = rows[0].row()
        size_item = self.files.item(row, 1)
        return self.files.item(row, 0).text(), int(getattr(size_item, "key", 0) or 0)

    @Slot()
    def _file_selection_changed(self) -> None:
        sel = self._selected_file()
        self.view_file_btn.setEnabled(sel is not None and is_text_file(sel[0]))
        if sel is not None and not is_text_file(sel[0]):
            self.file_hint.setText(f"{sel[0]} is a binary file; use Download or open it on huggingface.co")
        else:
            self.file_hint.setText("Double-click a .json, .md or other text file to read it")

    def _view_file(self) -> None:
        sel = self._selected_file()
        if sel is None:
            return
        path, size = sel
        if not is_text_file(path):
            QMessageBox.information(self, APP_NAME, f"{path} is not a text file ({hff.human(size)}).")
            return
        if self.file_dialog is None:
            self.file_dialog = FileViewerDialog(self)
        self.file_dialog.load(self.repo_id, path, size)

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802 - Qt API
        if self.worker is not None:
            self.worker.ready.disconnect(self._ready)
            self.worker = None
        if self.file_dialog is not None:
            self.file_dialog.close()
        event.accept()


TEXT_FILE_EXTS = {
    ".json", ".md", ".markdown", ".txt", ".yaml", ".yml", ".py", ".toml", ".cfg", ".ini", ".csv", ".tsv",
    ".jinja", ".jinja2", ".j2", ".modelfile", ".tiktoken", ".vocab", ".merges", ".license", ".rst", ".xml",
    ".html", ".htm", ".js", ".ts", ".sh", ".bat", ".cmd", ".ps1", ".cu", ".cpp", ".c", ".h", ".hpp", ".java",
    ".gitattributes", ".gitignore", ".model_card", ".log", ".bib", ".tex", ".properties", ".conf",
}
TEXT_FILE_NAMES = {"license", "readme", "modelfile", "notice", "changelog", "authors", "contributing", "version"}
MAX_TEXT_FILE_BYTES = 8 << 20


def is_text_file(path: str) -> bool:
    name = path.rsplit("/", 1)[-1]
    ext = os.path.splitext(name)[1].lower()
    if ext in TEXT_FILE_EXTS or name.lower() in TEXT_FILE_EXTS:  # the latter: dotfiles like .gitattributes
        return True
    return name.lower() in TEXT_FILE_NAMES or (not ext and name.lower().split(".")[0] in TEXT_FILE_NAMES)


class FileWorker(QThread):
    """Fetches one file of a repo straight from the Hub (not through the cache)."""

    ready = Signal(str, str, bytes)  # repo id, path, data
    failed = Signal(str, str, str)  # repo id, path, message

    def __init__(self, repo_id: str, path: str, parent=None) -> None:
        super().__init__(parent)
        self.repo_id = repo_id
        self.path = path

    def run(self) -> None:
        url = f"{HUB_URL}/{self.repo_id}/resolve/main/{urllib.parse.quote(self.path)}"
        try:
            data = fetch_url(url, limit=MAX_TEXT_FILE_BYTES + 1, timeout=60)
        except urllib.error.HTTPError as exc:
            self.failed.emit(self.repo_id, self.path, f"HTTP {exc.code}" + (" (gated or private; log in with `hf auth login`)" if exc.code in (401, 403) else ""))
            return
        except Exception as exc:  # noqa: BLE001
            self.failed.emit(self.repo_id, self.path, f"{type(exc).__name__}: {exc}")
            return
        if len(data) > MAX_TEXT_FILE_BYTES:
            self.failed.emit(self.repo_id, self.path, f"larger than {hff.human(MAX_TEXT_FILE_BYTES)}; open it on huggingface.co instead")
            return
        self.ready.emit(self.repo_id, self.path, data)


class FileViewerDialog(QDialog):
    """Shows one text file of a repo: JSON pretty-printed, Markdown rendered, anything else as text."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setWindowFlag(Qt.WindowType.Window, True)
        self.resize(860, 640)
        self.worker: FileWorker | None = None
        self.repo_id = ""
        self.path = ""
        root = QVBoxLayout(self)
        self.title = QLabel()
        self.title.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        root.addWidget(self.title)
        self.text = QPlainTextEdit()
        self.text.setReadOnly(True)
        self.text.setFont(QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont))
        self.text.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self.rendered = CardView()
        root.addWidget(self.text, 1)
        root.addWidget(self.rendered, 1)
        buttons = QHBoxLayout()
        self.raw_btn = QPushButton("Show source")
        self.raw_btn.setCheckable(True)
        self.raw_btn.toggled.connect(self._toggle_raw)
        copy_btn = QPushButton("Copy")
        copy_btn.clicked.connect(lambda: QApplication.clipboard().setText(self.text.toPlainText()))
        open_btn = QPushButton("Open on huggingface.co")
        open_btn.clicked.connect(lambda: QDesktopServices.openUrl(QUrl(f"{HUB_URL}/{self.repo_id}/blob/main/{urllib.parse.quote(self.path)}")))
        close_btn = QPushButton("Close")
        close_btn.clicked.connect(self.close)
        mark(close_btn, "primary")
        buttons.addWidget(self.raw_btn)
        buttons.addWidget(copy_btn)
        buttons.addWidget(open_btn)
        buttons.addStretch(1)
        buttons.addWidget(close_btn)
        root.addLayout(buttons)
        self._is_markdown = False

    def load(self, repo_id: str, path: str, size: int) -> None:
        self.repo_id, self.path = repo_id, path
        self.setWindowTitle(f"{path} - {repo_id}")
        self.title.setText(f"<b>{path}</b> ({hff.human(size)}) in {repo_id}: loading...")
        self.text.setPlainText("")
        self.rendered.set_card("")
        self._is_markdown = path.lower().endswith((".md", ".markdown"))
        self.raw_btn.setVisible(self._is_markdown)
        self.raw_btn.setChecked(False)
        self._toggle_raw(False)
        if self.worker is not None:
            self.worker.ready.disconnect(self._ready)
            self.worker.failed.disconnect(self._failed)
        self.worker = FileWorker(repo_id, path, self)
        self.worker.ready.connect(self._ready)
        self.worker.failed.connect(self._failed)
        self.worker.finished.connect(self.worker.deleteLater)
        self.worker.start()
        self.show()
        self.raise_()
        self.activateWindow()

    def _toggle_raw(self, raw: bool) -> None:
        show_rendered = self._is_markdown and not raw
        self.rendered.setVisible(show_rendered)
        self.text.setVisible(not show_rendered)

    @Slot(str, str, bytes)
    def _ready(self, repo_id: str, path: str, data: bytes) -> None:
        self.worker = None
        if (repo_id, path) != (self.repo_id, self.path):
            return
        text = data.decode("utf-8", "replace")
        kind = "text"
        if path.lower().endswith(".json"):
            try:
                text = json.dumps(json.loads(text), indent=2, ensure_ascii=False)
                kind = "JSON, pretty-printed"
            except ValueError:
                kind = "JSON (could not be parsed, shown as is)"
        elif self._is_markdown:
            kind = "Markdown, rendered"
            _meta, body = split_front_matter(text)
            body, _urls = absolutise(body, repo_id)
            self.rendered.set_card(body)
        self.text.setPlainText(text)
        self.title.setText(f"<b>{path}</b> ({hff.human(len(data))}, {kind}) in {repo_id}")

    @Slot(str, str, str)
    def _failed(self, repo_id: str, path: str, message: str) -> None:
        self.worker = None
        if (repo_id, path) != (self.repo_id, self.path):
            return
        self.title.setText(f"<b>{path}</b> in {repo_id}: <span style='color:#c5221f'>could not load: {message}</span>")

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802 - Qt API
        if self.worker is not None:
            self.worker.ready.disconnect(self._ready)
            self.worker.failed.disconnect(self._failed)
            self.worker = None
        event.accept()


# --------------------------------------------------------------------------- window

STATUS_COLORS = {
    "moved": "#1e8e3e",
    "complete": "#1e8e3e",
    "ready": "#1e8e3e",
    "downloaded": "#1e8e3e",
    "incomplete": "#b06000",
    "downloading": "#b06000",
    "checking": "#1a73e8",
    "error": "#c5221f",
    "skipped": "#c5221f",
    "interrupted": "#5f6368",
}


class TransferRow(QWidget):
    """One download in the progress panel: bar, big rate, heartbeat, details and a Stop button."""

    stop_requested = Signal(str)  # repo id

    def __init__(self, repo_id: str, parent=None) -> None:
        super().__init__(parent)
        self.repo_id = repo_id
        self.external = False
        self.pid = 0
        self.finished = False
        self.stall_logged = False
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 2, 0, 2)
        top = QHBoxLayout()
        self.bar = QProgressBar()
        self.bar.setRange(0, 1000)
        self.bar.setValue(0)
        self.bar.setFormat("%p%")
        self.bar.setMinimumHeight(24)
        self.rate_label = QLabel("—")
        font = QFont(self.rate_label.font())
        font.setPointSize(font.pointSize() + 5)
        font.setBold(True)
        self.rate_label.setFont(font)
        self.rate_label.setMinimumWidth(280)
        self.rate_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.rate_label.setToolTip("Bytes landing in the hub cache per second, as bytes and bits")
        self.stop_btn = QPushButton("Stop")
        self.stop_btn.clicked.connect(lambda: self.stop_requested.emit(self.repo_id))
        mark(self.stop_btn, "danger")
        top.addWidget(self.bar, 1)
        top.addWidget(self.rate_label)
        top.addWidget(self.stop_btn)
        info = QHBoxLayout()
        self.details = QLabel(f"{repo_id}: starting...")
        self.details.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.heartbeat = QLabel("")
        self.heartbeat.setToolTip(
            "Heartbeat: every second the meter checks that bytes are still landing in the cache "
            "and that the downloading process is still alive"
        )
        info.addWidget(self.details, 1)
        info.addWidget(self.heartbeat)
        root.addLayout(top)
        root.addLayout(info)

    def update_reading(self, t: Transfer) -> str | None:
        """Show a reading. Returns a line for the log when the heartbeat crosses a threshold."""
        self.external, self.pid = t.external, t.pid
        if t.external:
            self.stop_btn.setText(f"Stop other process (pid {t.pid})" if t.pid else "Stop other process")
            self.stop_btn.setEnabled(bool(t.pid) and psutil is not None)
        who = f"other process (pid {t.pid}) downloading {t.repo_id}" if t.external else t.label
        if t.total > 0:
            self.bar.setRange(0, 1000)
            self.bar.setValue(max(0, min(1000, int(1000 * t.done / t.total))))
            self.bar.setFormat(f"%p%  {hff.human(t.done)} / {hff.human(t.total)}")
        else:
            self.bar.setRange(0, 0)
            self.bar.setFormat(hff.human(t.done))
        hb_text, hb_color = heartbeat_text(t)
        self.heartbeat.setText(f"<b style='color:{hb_color}'>&#9679; {hb_text}</b>")
        if t.final:
            self.finished = True
            self.rate_label.setText("—")
            self.rate_label.setStyleSheet("")
            self.details.setText(f"{who}: ended with {hff.human(t.done)} in the cache")
            self.bar.setRange(0, 1000)
            self.bar.setValue(1000 if t.total and t.done >= t.total else self.bar.value())
            self.stop_btn.setEnabled(False)
            return None
        self.rate_label.setText(rate(t.speed) if t.speed > 0 else "0 B/s")
        self.rate_label.setStyleSheet("" if t.stalled < STALL_WARN_S else f"color: {hb_color}")
        bits = [who]
        if t.total > 0:
            bits.append(f"{hff.human(t.done)} of {hff.human(t.total)}")
            if t.speed > 0 and t.done < t.total:
                secs = int((t.total - t.done) / t.speed)
                eta = f"{secs // 3600}:{secs % 3600 // 60:02d}:{secs % 60:02d}" if secs >= 3600 else f"{secs // 60}:{secs % 60:02d}"
                bits.append(f"ETA {eta}")
        else:
            bits.append(f"{hff.human(t.done)} so far (size not known yet)")
        if t.down >= 0:
            bits.append(f"network down {rate(t.down)}, up {rate(t.up)}")
        self.details.setText("   ".join(bits))
        if t.stalled >= STALL_ALARM_S and not self.stall_logged:
            self.stall_logged = True
            return f"heartbeat: no data has landed for {who} in {int(t.stalled)} s"
        if t.stalled < STALL_WARN_S and self.stall_logged:
            self.stall_logged = False
            return f"heartbeat: {who} is receiving data again"
        return None


class ChecksumDialog(QDialog):
    """The pop-up at the end of a download: the three checksum stages, then every file's hashes."""

    def __init__(self, report: ChecksumReport, parent=None) -> None:
        super().__init__(parent)
        self.report = report
        self.setWindowTitle(f"{report.repo_id}: {'verified' if report.all_ok else 'verification'}")
        self.setWindowFlag(Qt.WindowType.Window, True)
        self.resize(900, 560)
        root = QVBoxLayout(self)
        title = QLabel(
            f"<b>{report.repo_id}</b>" + (f" @ {report.commit[:12]}" if report.commit else "")
            + (": <span style='color:#1e8e3e'>complete, all three checksum stages match</span>" if report.all_ok else ": <span style='color:#c5221f'>not fully verified</span>")
        )
        title.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        root.addWidget(title)
        for stage in ("expected", "after_download", "after_move"):
            ok, text = report.stage_verdict(stage)
            mark_txt = "&#10004;" if ok else ("&#8212;" if ok is None else "&#10008;")
            color = "#1e8e3e" if ok else ("#5f6368" if ok is None else "#c5221f")
            lab = QLabel(f"<span style='color:{color}; font-size: 14pt'>{mark_txt}</span> {text}")
            lab.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            root.addWidget(lab)
        extra = []
        if report.restarts:
            extra.append(f"the download was restarted {report.restarts} time(s) by the heartbeat")
        if report.rounds:
            extra.append(f"mismatching files were fetched again {report.rounds} time(s)")
        if extra:
            root.addWidget(QLabel("; ".join(extra)))

        table = QTableWidget(len(report.files), 6)
        table.setHorizontalHeaderLabels(["File", "Size", "Algorithm", "1. Hub (before)", "2. After download", "3. After move"])
        hdr = table.horizontalHeader()
        hdr.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        for col in range(1, 6):
            hdr.setSectionResizeMode(col, QHeaderView.ResizeMode.ResizeToContents)
        table.verticalHeader().setVisible(False)
        table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        table.setAlternatingRowColors(True)
        table.setSortingEnabled(True)
        table.setSortingEnabled(False)
        for row, f in enumerate(report.files):
            table.setItem(row, 0, QTableWidgetItem(f.path))
            table.setItem(row, 1, NumItem(hff.human(f.size), f.size))
            table.setItem(row, 2, QTableWidgetItem(f.algo))
            exp = QTableWidgetItem(f.expected[:20] + ("…" if len(f.expected) > 20 else "") if f.expected else "-")
            exp.setToolTip(f.expected)
            table.setItem(row, 3, exp)
            for col, stage in ((4, "after_download"), (5, "after_move")):
                got = getattr(f, stage)
                item = QTableWidgetItem((got[:20] + ("…" if len(got) > 20 else "")) if got else "-")
                item.setToolTip(got + (f"\n{f.note}" if f.note else ""))
                verdict = f.stage_ok(stage)
                if verdict is True:
                    tint_item(item, QColor("#1e8e3e"))
                elif verdict is False:
                    tint_item(item, QColor("#c5221f"))
                table.setItem(row, col, item)
        table.setSortingEnabled(True)
        root.addWidget(table, 1)

        buttons = QHBoxLayout()
        copy_btn = QPushButton("Copy report")
        copy_btn.clicked.connect(self._copy)
        close_btn = QPushButton("Close")
        close_btn.clicked.connect(self.close)
        mark(close_btn, "primary")
        buttons.addWidget(copy_btn)
        buttons.addStretch(1)
        buttons.addWidget(close_btn)
        root.addLayout(buttons)

    def _copy(self) -> None:
        r = self.report
        lines = [f"{r.repo_id} @ {r.commit}"]
        for stage in ("expected", "after_download", "after_move"):
            ok, text = r.stage_verdict(stage)
            lines.append(("OK   " if ok else ("--   " if ok is None else "FAIL ")) + text)
        lines.append("")
        for f in r.files:
            lines.append(f"{f.algo}\t{f.path}\t{f.expected or '-'}\t{f.after_download or '-'}\t{f.after_move or '-'}\t{f.note}")
        QApplication.clipboard().setText("\n".join(lines))
        self.statusTip = "copied"


class _Bridge(QObject):
    """Carries meter readings from a plain thread into the GUI thread."""

    transfer = Signal(object)


EXT_PID_RE = re.compile(r"pid (\d+)")


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle(APP_NAME)
        self.resize(1040, 760)
        self.settings = QSettings(APP_NAME, APP_NAME)
        self.workers: list[Worker] = []  # jobs running right now
        self.queue: list[QueueItem] = []  # downloads waiting for a slot
        self.interrupted: list[QueueItem] = []  # downloads that did not finish (this run or the last one)
        self.rows: dict[str, int] = {}
        self.card_dialog: ModelCardDialog | None = None
        self.ext_meters: dict[str, TransferMeter] = {}  # repo id -> watch on another process's download
        self.ext_bridge = _Bridge(self)
        self.ext_bridge.transfer.connect(self._on_transfer)
        self.transfer_rows: dict[str, TransferRow] = {}
        self.checksum_dialogs: list[QDialog] = []  # open verification pop-ups
        self.log_path = self._open_log_file()
        self._follow_repo = ""  # the model the open card window should switch to
        self._follow_timer = QTimer(self)
        self._follow_timer.setSingleShot(True)
        self._follow_timer.setInterval(250)  # let a Shift-drag settle before reloading the card
        self._follow_timer.timeout.connect(self._follow_fire)
        self._build_ui()
        self._build_menu()
        self._load_settings()
        self._set_running(False)

    # -- construction

    def _build_ui(self) -> None:
        self.tabs = QTabWidget()
        self.tabs.addTab(self._build_download_tab(), "Download")
        self.browse = BrowseTab(self)
        self.tabs.addTab(self.browse, "Browse Hub")
        self.library = LibraryTab(self)
        self.tabs.addTab(self.library, "Libraries")
        self.tabs.currentChanged.connect(self._tab_changed)
        self.setCentralWidget(self.tabs)

        self.progress_label = QLabel()
        self.busy = QProgressBar()
        self.busy.setRange(0, 0)
        self.busy.setMaximumWidth(140)
        self.busy.setTextVisible(False)
        self.statusBar().addPermanentWidget(self.progress_label, 1)
        self.statusBar().addPermanentWidget(self.busy)

    def _build_download_tab(self) -> QWidget:
        page = QWidget()
        root = QVBoxLayout(page)

        # download one model
        dl = QGroupBox("Download a model")
        dl_box = QVBoxLayout(dl)
        dl_row = QHBoxLayout()
        self.repo_edit = QLineEdit()
        self.repo_edit.setPlaceholderText("org/name, a huggingface.co link, or an 'hf download ...' command")
        self.repo_edit.returnPressed.connect(self.start_download)
        self.rev_edit = QLineEdit()
        self.rev_edit.setPlaceholderText("main")
        self.rev_edit.setMaximumWidth(160)
        self.finish_after_cb = QCheckBox("then move it to the library")
        self.finish_after_cb.setChecked(True)
        self.download_btn = QPushButton("Download")
        self.download_btn.setToolTip("Downloads into the hub cache; files that are already complete are skipped, so this also resumes")
        self.download_btn.clicked.connect(self.start_download)
        mark(self.download_btn, "primary")
        self.resume_btn = QPushButton("Resume")
        self.resume_btn.setToolTip(
            "Pick up a stopped download of this repo: complete files are kept, stale partial files are "
            "removed and the rest is fetched again, then the model is verified and moved"
        )
        self.resume_btn.clicked.connect(self.start_resume_field)
        dl_row.addWidget(QLabel("Repo:"))
        dl_row.addWidget(self.repo_edit, 1)
        dl_row.addWidget(QLabel("Revision:"))
        dl_row.addWidget(self.rev_edit)
        dl_row.addWidget(self.finish_after_cb)
        dl_row.addWidget(self.download_btn)
        dl_row.addWidget(self.resume_btn)
        dl_box.addLayout(dl_row)
        hint = QLabel(
            "Accepts  org/name,  hf download org/name,  https://huggingface.co/org/name?clone=true,  "
            ".../tree/&lt;revision&gt;  and  git clone ... forms. A download that stops is verified file "
            "by file; click Resume to pick it up where it stopped."
        )
        hint.setWordWrap(True)
        hint.setEnabled(False)  # the disabled text colour reads as a hint in light and dark themes
        dl_box.addWidget(hint)

        # a pasted list
        list_row = QHBoxLayout()
        self.list_edit = QPlainTextEdit()
        self.list_edit.setPlaceholderText(
            "Or paste a list: one repo id, link or 'hf download ...' command per line. "
            "They are downloaded one after the other."
        )
        self.list_edit.setMaximumHeight(96)
        self.list_edit.textChanged.connect(self._list_changed)
        list_btns = QVBoxLayout()
        self.download_all_btn = QPushButton("Download all")
        self.download_all_btn.setToolTip("Download every repo in the list, in order, then verify and move each one")
        self.download_all_btn.clicked.connect(self.start_download_list)
        mark(self.download_all_btn, "primary")
        self.clear_list_btn = QPushButton("Clear list")
        self.clear_list_btn.clicked.connect(self.list_edit.clear)
        self.list_count = QLabel("")
        self.list_count.setEnabled(False)
        self.parallel_spin = QSpinBox()
        self.parallel_spin.setRange(1, 8)
        self.parallel_spin.setValue(3)
        self.parallel_spin.setPrefix("download ")
        self.parallel_spin.setSuffix(" at once")
        self.parallel_spin.setToolTip("How many models are downloaded at the same time; the rest wait in the queue")
        self.parallel_spin.valueChanged.connect(lambda _v: self._pump_queue())
        list_btns.addWidget(self.download_all_btn)
        list_btns.addWidget(self.clear_list_btn)
        list_btns.addWidget(self.parallel_spin)
        list_btns.addWidget(self.list_count)
        list_btns.addStretch(1)
        list_row.addWidget(self.list_edit, 1)
        list_row.addLayout(list_btns)
        dl_box.addLayout(list_row)

        # downloads that did not finish (last time or this time): one click resumes them all
        self.resume_banner = QWidget()
        banner = QHBoxLayout(self.resume_banner)
        banner.setContentsMargins(0, 0, 0, 0)
        self.resume_banner_label = QLabel()
        self.resume_banner_label.setWordWrap(True)
        self.resume_all_btn = QPushButton("Resume all")
        self.resume_all_btn.setToolTip("Pick every unfinished download up where it stopped")
        self.resume_all_btn.clicked.connect(self.resume_interrupted)
        mark(self.resume_all_btn, "primary")
        self.dismiss_btn = QPushButton("Forget them")
        self.dismiss_btn.setToolTip("Stop remembering these; the files already in the cache stay there")
        self.dismiss_btn.clicked.connect(self.dismiss_interrupted)
        banner.addWidget(self.resume_banner_label, 1)
        banner.addWidget(self.resume_all_btn)
        banner.addWidget(self.dismiss_btn)
        self.resume_banner.setVisible(False)
        dl_box.addWidget(self.resume_banner)
        root.addWidget(dl)

        # finish cached models
        fin = QGroupBox("Finish and move cached models")
        form = QFormLayout(fin)
        self.cache_edit = QLineEdit()
        self.cache_edit.setPlaceholderText(str(hff.constants.HF_HUB_CACHE))
        form.addRow("Hub cache:", self._path_row(self.cache_edit, "Choose the Hugging Face hub cache"))
        self.dest_edit = QLineEdit()
        self.dest_edit.setPlaceholderText(str(hff.DEFAULT_DEST))
        self.dest_edit.setToolTip("Type a folder or browse; it applies as soon as you leave the field")
        self.dest_edit.editingFinished.connect(self._dest_changed)
        form.addRow("Library folder:", self._path_row(self.dest_edit, "Choose where models go"))
        self.filter_edit = QLineEdit()
        self.filter_edit.setPlaceholderText("all models; or parts of repo ids, separated by spaces")
        form.addRow("Only repos matching:", self.filter_edit)

        self.layout_combo = QComboBox()
        self.layout_combo.addItems(["flat", "nested"])
        self.layout_combo.setToolTip("flat: <library>/<name>   nested: <library>/<org>/<name>")
        opts1 = QHBoxLayout()
        opts1.addWidget(QLabel("Folder layout:"))
        opts1.addWidget(self.layout_combo)
        self.merge_cb = QCheckBox("Merge into existing folders")
        self.merge_cb.setToolTip("Move files into a destination folder that already exists (--merge)")
        self.checksum_cb = QCheckBox("Verify checksums (slow)")
        self.checksum_cb.setToolTip("Hash every file against the Hub before moving it (--checksum)")
        opts1.addSpacing(16)
        opts1.addWidget(self.merge_cb)
        opts1.addStretch(1)
        form.addRow("Options:", opts1)

        opts2 = QHBoxLayout()
        self.no_download_cb = QCheckBox("Skip downloads")
        self.no_download_cb.setToolTip("Do not resume anything, only move models that are already complete (--no-download)")
        self.no_move_cb = QCheckBox("Skip moving")
        self.no_move_cb.setToolTip("Only check and resume, leave everything in the cache (--no-move)")
        opts2.addWidget(self.no_download_cb)
        opts2.addWidget(self.no_move_cb)
        opts2.addWidget(self.checksum_cb)
        opts2.addStretch(1)
        form.addRow("", opts2)

        opts3 = QHBoxLayout()
        self.wait_cb = QCheckBox("Wait for other downloads, poll every")
        self.wait_cb.setToolTip(
            "A repo that another process is downloading is normally left alone; "
            "wait for it and move it when the download ends (--wait)"
        )
        self.poll_spin = QSpinBox()
        self.poll_spin.setRange(5, 3600)
        self.poll_spin.setValue(30)
        self.poll_spin.setSuffix(" s")
        self.wait_cb.toggled.connect(self.poll_spin.setEnabled)
        self.poll_spin.setEnabled(False)
        opts3.addWidget(self.wait_cb)
        opts3.addWidget(self.poll_spin)
        opts3.addStretch(1)
        form.addRow("", opts3)

        buttons = QHBoxLayout()
        self.scan_btn = QPushButton("Check (dry run)")
        self.scan_btn.setToolTip("Verify every cached model file by file and report, change nothing")
        self.scan_btn.clicked.connect(lambda: self.start_finish(dry_run=True))
        self.run_btn = QPushButton("Finish and move")
        self.run_btn.setToolTip("Resume incomplete downloads and move complete models out of the cache")
        self.run_btn.clicked.connect(lambda: self.start_finish(dry_run=False))
        self.run_btn.setDefault(True)
        mark(self.run_btn, "primary")
        self.stop_btn = QPushButton("Stop")
        self.stop_btn.clicked.connect(self.stop)
        mark(self.stop_btn, "danger")
        self.clear_btn = QPushButton("Clear log")
        self.clear_btn.setToolTip("Clears the log shown here; the log file keeps everything")
        self.clear_btn.clicked.connect(self.log_view_clear)
        self.open_log_btn = QPushButton("Open log")
        self.open_log_btn.setToolTip("Open the log file (every line of every session, including all checksums) in your editor")
        self.open_log_btn.clicked.connect(self.open_log)
        buttons.addWidget(self.scan_btn)
        buttons.addWidget(self.run_btn)
        buttons.addWidget(self.stop_btn)
        buttons.addStretch(1)
        buttons.addWidget(self.open_log_btn)
        buttons.addWidget(self.clear_btn)
        form.addRow(buttons)
        root.addWidget(fin)

        # progress of every running download, one row each (ours and other processes')
        prog_box = QGroupBox("Download progress")
        self.transfer_layout = QVBoxLayout(prog_box)
        self.transfer_idle = QLabel("No download running.")
        self.transfer_idle.setEnabled(False)
        self.transfer_layout.addWidget(self.transfer_idle)
        root.addWidget(prog_box)

        # results: table above, log below
        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["Status", "Repo", "Size", "Note"])
        header = self.table.horizontalHeader()
        header.setMinimumSectionSize(90)
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setAlternatingRowColors(True)

        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setMaximumBlockCount(20000)
        self.log_view.setFont(QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont))
        self.log_view.setPlaceholderText("Log output appears here.")

        split = QSplitter(Qt.Orientation.Vertical)
        split.addWidget(self.table)
        split.addWidget(self.log_view)
        split.setStretchFactor(0, 2)
        split.setStretchFactor(1, 3)
        root.addWidget(split, 1)
        self.splitter = split
        return page

    def _path_row(self, edit: QLineEdit, title: str) -> QWidget:
        box = QWidget()
        row = QHBoxLayout(box)
        row.setContentsMargins(0, 0, 0, 0)
        btn = QPushButton("Browse...")

        def browse() -> None:
            start = Path(edit.text().strip() or edit.placeholderText())  # the last used folder
            while not start.is_dir() and start.parent != start:
                start = start.parent
            chosen = QFileDialog.getExistingDirectory(self, title, str(start))
            if chosen:
                edit.setText(os.path.normpath(chosen))

        btn.clicked.connect(browse)
        row.addWidget(edit, 1)
        row.addWidget(btn)
        return box

    def _build_menu(self) -> None:
        file_menu = self.menuBar().addMenu("&File")
        act = QAction("Open &library folder", self)
        act.triggered.connect(lambda: self._open_folder(str(self._options().dest_path())))
        file_menu.addAction(act)
        act = QAction("Open hub &cache folder", self)
        act.triggered.connect(lambda: self._open_folder(str(self._options().cache_path())))
        file_menu.addAction(act)
        file_menu.addSeparator()
        act = QAction("Open lo&g file", self)
        act.setShortcut("Ctrl+L")
        act.triggered.connect(self.open_log)
        file_menu.addAction(act)
        act = QAction("Open log f&older", self)
        act.triggered.connect(lambda: self._open_folder(str(self.log_path.parent)))
        file_menu.addAction(act)
        file_menu.addSeparator()
        act = QAction("&Quit", self)
        act.setShortcut("Ctrl+Q")
        act.triggered.connect(self.close)
        file_menu.addAction(act)

        help_menu = self.menuBar().addMenu("&Help")
        act = QAction("&About " + APP_NAME, self)
        act.triggered.connect(self._about)
        help_menu.addAction(act)

    # -- settings

    def _load_settings(self) -> None:
        s = self.settings
        self.cache_edit.setText(s.value("cache_dir", "", str))
        self.dest_edit.setText(s.value("dest", "", str))
        self.library.where.setText(self.dest_edit.text())  # the Libraries tab starts at the last used folder
        self.filter_edit.setText(s.value("filters", "", str))
        self.layout_combo.setCurrentText(s.value("layout", "flat", str))
        self.merge_cb.setChecked(s.value("merge", False, bool))
        # checksum verification is on by default; settings saved by 1.1 (default off) are migrated once
        if s.value("checksum_default_on", False, bool):
            self.checksum_cb.setChecked(s.value("checksum", True, bool))
        else:
            self.checksum_cb.setChecked(True)
            s.setValue("checksum_default_on", True)
        self.no_download_cb.setChecked(s.value("no_download", False, bool))
        self.no_move_cb.setChecked(s.value("no_move", False, bool))
        self.wait_cb.setChecked(s.value("wait", False, bool))
        self.poll_spin.setValue(s.value("poll", 30, int))
        self.finish_after_cb.setChecked(s.value("finish_after", True, bool))
        self.list_edit.setPlainText(s.value("download_list", "", str))
        self.parallel_spin.setValue(s.value("parallel", 3, int))
        try:
            saved = json.loads(s.value("unfinished", "[]", str))
        except ValueError:
            saved = []
        self.interrupted = [QueueItem.from_dict(d) for d in saved if isinstance(d, dict) and d.get("repo_id")]
        self._update_banner()
        b = self.browse
        b.search_edit.setText(s.value("browse_search", "", str))
        b.author_edit.setText(s.value("browse_author", "", str))
        b.task_combo.setCurrentText(s.value("browse_task", "", str))
        b.tag_edit.setText(s.value("browse_tags", "", str))
        b.sort_combo.setCurrentIndex(s.value("browse_sort", 0, int))
        b.page_spin.setValue(s.value("browse_page_size", 30, int))
        geometry = s.value("geometry")
        if geometry is not None:
            self.restoreGeometry(geometry)
        splitter = s.value("splitter")
        if splitter is not None:
            self.splitter.restoreState(splitter)

    def _save_settings(self) -> None:
        s = self.settings
        o = self._options()
        s.setValue("cache_dir", o.cache_dir)
        s.setValue("dest", o.dest)
        s.setValue("filters", self.filter_edit.text())
        s.setValue("layout", o.layout)
        s.setValue("merge", o.merge)
        s.setValue("checksum", o.checksum)
        s.setValue("no_download", o.no_download)
        s.setValue("no_move", o.no_move)
        s.setValue("wait", o.wait)
        s.setValue("poll", o.poll)
        s.setValue("finish_after", self.finish_after_cb.isChecked())
        s.setValue("download_list", self.list_edit.toPlainText())
        s.setValue("parallel", self.parallel_spin.value())
        self._save_unfinished()
        b = self.browse
        s.setValue("browse_search", b.search_edit.text())
        s.setValue("browse_author", b.author_edit.text())
        s.setValue("browse_task", b.task_combo.currentText())
        s.setValue("browse_tags", b.tag_edit.text())
        s.setValue("browse_sort", b.sort_combo.currentIndex())
        s.setValue("browse_page_size", b.page_spin.value())
        s.setValue("geometry", self.saveGeometry())
        s.setValue("splitter", self.splitter.saveState())

    def _options(self, dry_run: bool = False) -> Options:
        return Options(
            cache_dir=self.cache_edit.text().strip(),
            dest=self.dest_edit.text().strip(),
            layout=self.layout_combo.currentText(),
            filters=[f for f in self.filter_edit.text().replace(",", " ").split() if f],
            dry_run=dry_run,
            no_download=self.no_download_cb.isChecked(),
            no_move=self.no_move_cb.isChecked(),
            merge=self.merge_cb.isChecked(),
            checksum=self.checksum_cb.isChecked(),
            wait=self.wait_cb.isChecked(),
            poll=self.poll_spin.value(),
            no_queue=WINDOWS,  # hfq is a POSIX tool
        )

    # -- actions

    @Slot()
    def start_download(self) -> None:
        try:
            repo_id, revision = parse_repo_ref(self.repo_edit.text())
        except ValueError as exc:
            QMessageBox.warning(self, APP_NAME, str(exc))
            return
        self.repo_edit.setText(repo_id)
        if revision:
            self.rev_edit.setText(revision)
        self.download_repo(repo_id, self.rev_edit.text().strip())

    def local_state(self, repo_id: str) -> LocalState:
        opts = self._options()
        return LocalIndex(opts.dest_path(), opts.cache_path(), opts.layout).lookup(repo_id)

    def _confirm_redownload(self, repo_id: str, local: LocalState) -> bool:
        """Ask before fetching a model that is already on this machine."""
        where = str(local.path) if local.path else local.state
        answer = QMessageBox.question(
            self,
            APP_NAME,
            f"{repo_id} has already been downloaded.\n\nIt is {local.state}:\n{where}\n\n"
            "Are you sure you want to download it again? Files that are already complete are skipped, "
            "so this only fetches what changed on the Hub.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        return answer == QMessageBox.StandardButton.Yes

    def download_repo(self, repo_id: str, revision: str = "") -> None:
        """Download one repo (from the field, the Hub browser or the model card window).

        Downloads run side by side, up to the "at once" limit; the rest queue up.
        """
        if self._exclusive_running():
            QMessageBox.information(self, APP_NAME, "A pass over the whole cache is running. Wait for it or stop it first.")
            return
        if repo_id in self.active_repos():
            self.statusBar().showMessage(f"{repo_id} is already being downloaded (or waiting in the queue)", 5000)
            return
        local = self.local_state(repo_id)
        if local.downloaded and not self._confirm_redownload(repo_id, local):
            return
        self.show_download_tab()
        self.repo_edit.setText(repo_id)
        self.rev_edit.setText(revision)
        self._enqueue([(repo_id, revision)])

    # -- the download queue

    def _exclusive_running(self) -> bool:
        return any(w.kind == "exclusive" for w in self.workers)

    def active_repos(self) -> set[str]:
        """Repos being downloaded, verified or resumed right now, plus the queue."""
        active = {r for w in self.workers for r in w.repo_ids}
        active.update(item.repo_id for item in self.queue)
        return active

    def _downloads_in_flight(self) -> int:
        return sum(1 for w in self.workers if w.ctx is not None and w.ctx[0] == "download")

    def _enqueue(self, items: list[tuple[str, str]], mode: str = "download") -> None:
        if not self.workers and not self.queue:
            self._clear_results()
        active = self.active_repos()
        added = 0
        for repo_id, revision in items:
            if repo_id in active:
                continue
            active.add(repo_id)
            self.queue.append(QueueItem(repo_id, revision, mode))
            self.interrupted = [it for it in self.interrupted if it.repo_id != repo_id]
            self._set_row(repo_id, "queued", "waiting for a download slot", None)
            added += 1
        if added > 1:
            self.append_line(f"== {added} repos queued, {self.parallel_spin.value()} at once")
        self._pump_queue()
        self._set_running(bool(self.workers))
        self._save_unfinished()
        self._update_banner()

    def _pump_queue(self) -> None:
        """Start queued downloads while there are free slots."""
        while self.queue and self._downloads_in_flight() < self.parallel_spin.value() and not self._exclusive_running():
            item = self.queue.pop(0)
            opts = self._options()
            finish_after = self.finish_after_cb.isChecked()
            repo_dir = opts.cache_path() / f"models--{item.repo_id.replace('/', '--')}"
            if item.mode == "resume" and (repo_dir / "snapshots").is_dir():
                # pick a cached, unfinished repo up: hffinish's resume pass limited to it (stale
                # partials dropped, missing files fetched at the cached commit), then the same
                # three checksum stages and the move as for a fresh download
                # a partial file written in the last two minutes looks like a live download to
                # hffinish (it cannot know the process is gone); wait that out and then go on
                opts.wait = True
                opts.poll = 10
                self._start(
                    download_job(item.repo_id, item.revision, opts, not opts.no_move, resume=True),
                    f"resuming {item.repo_id}",
                    repo_ids=[item.repo_id],
                    ctx=("download", [item.repo_id], not opts.no_move, item.revision),
                )
                continue
            if item.mode == "resume":
                self.append_line(f"{item.repo_id} is not in the cache yet, starting the download")
            title = f"hf download {item.repo_id}" + (f" --revision {item.revision}" if item.revision else "")
            if finish_after:
                title += ", then hffinish " + " ".join(opts.argv(filters=[item.repo_id]))
            self._start(
                download_job(item.repo_id, item.revision, opts, finish_after),
                title,
                repo_ids=[item.repo_id],
                ctx=("download", [item.repo_id], finish_after, item.revision),
            )

    # -- remembering unfinished downloads across restarts

    def _save_unfinished(self) -> None:
        items: dict[str, QueueItem] = {}
        for w in self.workers:
            if w.ctx is not None and w.ctx[0] == "download":
                for repo_id in w.ctx[1]:
                    items[repo_id] = QueueItem(repo_id, w.ctx[3] if len(w.ctx) > 3 else "", "resume")
        for item in self.queue:
            items.setdefault(item.repo_id, QueueItem(item.repo_id, item.revision, "resume"))
        for item in self.interrupted:
            items.setdefault(item.repo_id, item)
        self.settings.setValue("unfinished", json.dumps([it.as_dict() for it in items.values()]))

    def _update_banner(self) -> None:
        if not self.interrupted:
            self.resume_banner.setVisible(False)
            return
        names = ", ".join(it.repo_id for it in self.interrupted[:4])
        if len(self.interrupted) > 4:
            names += f" and {len(self.interrupted) - 4} more"
        self.resume_banner_label.setText(
            f"<b>{len(self.interrupted)} download(s) did not finish:</b> {names}. "
            "Files already fetched are kept; resuming fetches only what is missing."
        )
        self.resume_banner_label.setToolTip("\n".join(it.repo_id for it in self.interrupted))
        self.resume_all_btn.setText("Resume all" if len(self.interrupted) > 1 else "Resume")
        self.resume_banner.setVisible(True)

    @Slot()
    def resume_interrupted(self) -> None:
        if self._exclusive_running():
            self.statusBar().showMessage("Waiting for the cache check to finish, the downloads start right after it.", 6000)
        items = [(it.repo_id, it.revision) for it in self.interrupted]
        self.show_download_tab()
        self._enqueue(items, mode="resume")

    @Slot()
    def dismiss_interrupted(self) -> None:
        self.interrupted = []
        self._save_unfinished()
        self._update_banner()

    @Slot()
    def start_download_list(self) -> None:
        items, rejected = parse_repo_list(self.list_edit.toPlainText())
        if rejected:
            shown = "\n".join(f"{line}\n    {why}" for line, why in rejected[:8])
            if len(rejected) > 8:
                shown += f"\n... and {len(rejected) - 8} more"
            if not items:
                QMessageBox.warning(self, APP_NAME, "No usable line in the list:\n\n" + shown)
                return
            answer = QMessageBox.question(
                self,
                APP_NAME,
                f"{len(rejected)} line(s) are not repo ids or links and will be skipped:\n\n{shown}\n\n"
                f"Download the other {len(items)}?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.Yes,
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
        if not items:
            QMessageBox.information(self, APP_NAME, "Paste one repo id or link per line into the list first.")
            return
        self.download_many(items)

    def download_many(self, items: list[tuple[str, str]]) -> None:
        """Queue several repos (from the pasted list or a multi-selection in the Hub browser),
        asking first about the ones that are already downloaded."""
        if self._exclusive_running():
            QMessageBox.information(self, APP_NAME, "A pass over the whole cache is running. Wait for it or stop it first.")
            return
        opts = self._options()
        index = LocalIndex(opts.dest_path(), opts.cache_path(), opts.layout)
        already = [(repo_id, index.lookup(repo_id)) for repo_id, _rev in items]
        already = [(r, s) for r, s in already if s.downloaded]
        if already:
            shown = "\n".join(f"{r}  ({s.state})" for r, s in already[:10])
            if len(already) > 10:
                shown += f"\n... and {len(already) - 10} more"
            box = QMessageBox(self)
            box.setIcon(QMessageBox.Icon.Question)
            box.setWindowTitle(APP_NAME)
            box.setText(f"{len(already)} of the {len(items)} selected repo(s) have already been downloaded:\n\n{shown}")
            box.setInformativeText("Are you sure you want to download them again?")
            again = box.addButton("Download again", QMessageBox.ButtonRole.YesRole)
            skip = box.addButton("Skip those", QMessageBox.ButtonRole.NoRole)
            box.addButton(QMessageBox.StandardButton.Cancel)
            box.setDefaultButton(skip)
            box.exec()
            clicked = box.clickedButton()
            if clicked is skip:
                done = {r for r, _s in already}
                items = [it for it in items if it[0] not in done]
                if not items:
                    self.statusBar().showMessage("Everything selected is already downloaded.", 6000)
                    return
            elif clicked is not again:
                return
        self.show_download_tab()
        self._enqueue(items)

    def add_to_list(self, repo_id: str) -> None:
        """Append a repo to the list on the Download tab (from the Hub browser or the card window)."""
        current = self.list_edit.toPlainText()
        if any(line.strip() == repo_id for line in current.splitlines()):
            self.statusBar().showMessage(f"{repo_id} is already in the list", 4000)
            return
        self.list_edit.setPlainText((current.rstrip("\n") + "\n" if current.strip() else "") + repo_id + "\n")
        self.statusBar().showMessage(f"added {repo_id} to the list on the Download tab", 4000)

    @Slot()
    def _list_changed(self) -> None:
        items, rejected = parse_repo_list(self.list_edit.toPlainText())
        text = ""
        if items or rejected:
            text = f"{len(items)} repo(s)" + (f", {len(rejected)} bad line(s)" if rejected else "")
        self.list_count.setText(text)

    def show_details(self, repo_id: str) -> None:
        if self.card_dialog is None:
            self.card_dialog = ModelCardDialog(self)
        self.card_dialog.load(repo_id)

    def follow_selection(self, repo_id: str | None) -> None:
        """Selected another model while the card window is open: the window switches to it."""
        dlg = self.card_dialog
        if not repo_id or dlg is None or not dlg.isVisible() or dlg.repo_id == repo_id:
            return
        self._follow_repo = repo_id
        self._follow_timer.start()

    @Slot()
    def _follow_fire(self) -> None:
        dlg = self.card_dialog
        if dlg is not None and dlg.isVisible() and self._follow_repo and dlg.repo_id != self._follow_repo:
            dlg.load(self._follow_repo, raise_window=False)

    def start_finish(self, dry_run: bool, title: str = "") -> None:
        """A pass over the whole cache: runs alone."""
        opts = self._options(dry_run=dry_run)
        self._start(
            finish_job(opts),
            (title + ": " if title else "") + "hffinish " + " ".join(opts.argv()),
            kind="exclusive",
            clear=True,
        )

    def start_verify(self, repo_ids: str | list[str]) -> None:
        """Check repos in the cache file by file (a dry run limited to them); runs beside downloads."""
        ids = [repo_ids] if isinstance(repo_ids, str) else list(repo_ids)
        if self._exclusive_running():
            return
        opts = self._options(dry_run=True)
        opts.filters = ids
        self._start(
            finish_job(opts),
            f"verifying {', '.join(ids)}: hffinish " + " ".join(opts.argv()),
            repo_ids=ids,
            ctx=("verify", ids),
        )

    def start_resume(self, repo_id: str, revision: str = "") -> None:
        """Pick a download up where it stopped.

        For a repo that is in the cache this is a finish pass limited to it:
        complete files are kept, stale partial files are dropped (huggingface_hub
        never appends to them), the rest is downloaded again at the cached
        commit, then the model is verified and moved. A repo that is not in the
        cache yet is simply downloaded.
        """
        if repo_id in self.active_repos():
            self.statusBar().showMessage(f"{repo_id} is already being worked on", 5000)
            return
        self.show_download_tab()
        self.repo_edit.setText(repo_id)
        self._enqueue([(repo_id, revision)], mode="resume")

    @Slot()
    def start_resume_field(self) -> None:
        try:
            repo_id, revision = parse_repo_ref(self.repo_edit.text())
        except ValueError as exc:
            QMessageBox.warning(self, APP_NAME, str(exc))
            return
        self.repo_edit.setText(repo_id)
        if revision:
            self.rev_edit.setText(revision)
        self.start_resume(repo_id, self.rev_edit.text().strip())

    def show_download_tab(self) -> None:
        self.tabs.setCurrentIndex(0)

    @Slot()
    def _dest_changed(self) -> None:
        """The library folder was typed or chosen: apply it everywhere, no restart needed."""
        text = self.dest_edit.text().strip()
        if text:
            text = os.path.normpath(os.path.expanduser(text))
            if text != self.dest_edit.text():
                self.dest_edit.setText(text)
        if not self.library.where.hasFocus():
            self.library.where.setText(text)
        self.settings.setValue("dest", text)
        dest = self._options().dest_path()
        self.statusBar().showMessage(
            f"library folder: {dest}" + ("" if dest.is_dir() else " (does not exist yet; it is created when a model is moved there)"),
            8000,
        )
        self.library.refresh()
        self.browse.refresh_local()

    @Slot()
    def stop(self) -> None:
        """Stop everything: empty the queue and cancel every running job (all stay remembered as unfinished)."""
        for item in self.queue:
            self._set_row(item.repo_id, "stopped", "removed from the queue; remembered as unfinished", None)
            self._remember_interrupted(item.repo_id, item.revision)
        self.queue.clear()
        if self.workers:
            self.append_line("stopping...")
        for w in list(self.workers):
            w.cancel()
        self._set_running(bool(self.workers))
        self._save_unfinished()
        self._update_banner()

    def _stop_repo(self, repo_id: str) -> None:
        """The Stop button of one progress row."""
        for w in self.workers:
            if w.current_repo == repo_id or repo_id in w.repo_ids:
                self.append_line(f"stopping {repo_id}...")
                w.cancel()
                return
        queued = [item for item in self.queue if item.repo_id == repo_id]
        if queued:
            self.queue = [item for item in self.queue if item.repo_id != repo_id]
            self._set_row(repo_id, "stopped", "removed from the queue; remembered as unfinished", None)
            self._remember_interrupted(repo_id, queued[0].revision)
            return
        if repo_id in self.ext_meters:
            self._kill_external(repo_id)

    def _remember_interrupted(self, repo_id: str, revision: str = "") -> None:
        if all(it.repo_id != repo_id for it in self.interrupted):
            self.interrupted.append(QueueItem(repo_id, revision, "resume"))

    def _clear_results(self) -> None:
        self.table.setRowCount(0)
        self.rows.clear()

    def _start(self, job, title: str, *, kind: str = "download", repo_ids=(), ctx=None, clear: bool = False) -> Worker | None:
        if kind == "exclusive" and (self.workers or self.queue):
            QMessageBox.information(self, APP_NAME, "Wait for the running downloads to finish, or stop them, before a pass over the whole cache.")
            return None
        if self._exclusive_running():
            self.statusBar().showMessage("A pass over the whole cache is running; wait for it or stop it.", 5000)
            return None
        self._save_settings()
        if clear:
            self._clear_results()
        self.append_line(f"== {title}")
        w = Worker(job, self._options().cache_path(), self)
        w.kind = kind
        w.repo_ids = list(repo_ids)
        w.ctx = ctx
        w.line.connect(self.append_line)
        w.progress.connect(self.progress_label.setText)
        w.repos_found.connect(self._on_repos_found)
        w.repo_update.connect(self._on_repo_update)
        w.transfer.connect(self._on_transfer)
        w.external.connect(self._on_external)
        w.checksums.connect(self._on_checksums)
        w.done.connect(lambda rc, w=w: self._on_done(w, rc))
        self.workers.append(w)
        self._set_running(True)
        w.start()
        return w

    # -- progress panel

    @Slot(object)
    def _on_checksums(self, report: ChecksumReport) -> None:
        """A download finished (or gave up): log the three verdicts and show them in a pop-up."""
        for stage in ("expected", "after_download", "after_move"):
            ok, text = report.stage_verdict(stage)
            self.append_line(("OK   " if ok else ("??   " if ok is None else "FAIL ")) + text)
        dlg = ChecksumDialog(report, self)
        self.checksum_dialogs.append(dlg)
        dlg.finished.connect(lambda _r, d=dlg: self.checksum_dialogs.remove(d) if d in self.checksum_dialogs else None)
        dlg.show()
        dlg.raise_()

    def _transfer_row(self, repo_id: str) -> TransferRow:
        row = self.transfer_rows.get(repo_id)
        if row is not None and row.finished:
            self._drop_transfer_row(repo_id, row)
            row = None
        if row is None:
            row = TransferRow(repo_id)
            row.stop_requested.connect(self._stop_repo)
            self.transfer_rows[repo_id] = row
            self.transfer_layout.addWidget(row)
            self.transfer_idle.setVisible(False)
        return row

    def _drop_transfer_row(self, repo_id: str, row: TransferRow) -> None:
        if self.transfer_rows.get(repo_id) is not row:
            return  # already replaced by a newer download of the same repo
        del self.transfer_rows[repo_id]
        self.transfer_layout.removeWidget(row)
        row.deleteLater()
        if not self.transfer_rows:
            self.transfer_idle.setVisible(True)

    @Slot(object)
    def _on_transfer(self, t: Transfer) -> None:
        row = self._transfer_row(t.repo_id)
        line = row.update_reading(t)
        if line:
            self.append_line(line)
        if t.final:
            if t.external:
                self._external_ended(t)
            QTimer.singleShot(15000, lambda rid=t.repo_id, row=row: self._drop_transfer_row(rid, row))

    @Slot(str, str)
    def _on_external(self, repo_id: str, reason: str) -> None:
        """hffinish found another process downloading this repo: show that download's progress."""
        current = self.ext_meters.get(repo_id)
        if current is not None and current.is_alive():
            return
        m = EXT_PID_RE.search(reason)
        pid = int(m.group(1)) if m else 0
        cache = self._options().cache_path()
        repo_dir = cache / f"models--{repo_id.replace('/', '--')}"

        def alive() -> bool:
            if pid:
                return pid_alive(pid)
            # no pid known (lock or fresh partial file): alive while either sign persists
            for lock in (cache / ".locks" / repo_dir.name).glob("*.lock"):
                if hff.lock_is_held(lock):
                    return True
            now = time.time()
            for part in (repo_dir / "blobs").glob("*.incomplete"):
                try:
                    if now - part.stat().st_mtime < hff.ACTIVE_WINDOW_S:
                        return True
                except OSError:
                    continue
            return False

        def expected() -> int:
            return cached_expected_size(repo_id, cache) or expected_size(repo_id, "")

        meter = TransferMeter(
            self.ext_bridge.transfer.emit, repo_id, repo_id, cache, expected, alive=alive, external=True, pid=pid
        )
        self.ext_meters[repo_id] = meter
        meter.start()
        why = re.search(r"\((.*)\)", reason)
        self.append_line(f"watching the other process's download of {repo_id} ({why.group(1) if why else reason})")

    def stop_external(self, repo_id: str | None = None) -> None:
        """Stop watching one repo's external download, or all of them (no closing reading is shown)."""
        ids = [repo_id] if repo_id else list(self.ext_meters)
        for rid in ids:
            meter = self.ext_meters.pop(rid, None)
            if meter is not None:
                meter.stop(final=False)

    def _external_ended(self, t: Transfer) -> None:
        meter = self.ext_meters.get(t.repo_id)
        if meter is None or meter.pid != t.pid:
            return  # a reading from a watch that was already replaced or stopped
        del self.ext_meters[t.repo_id]
        self.append_line(f"the other process's download of {t.repo_id} has ended ({hff.human(t.done)} in the cache); verifying")
        self.library.refresh()
        self.browse.refresh_local()
        if not self._exclusive_running() and t.repo_id not in self.active_repos():
            QTimer.singleShot(0, lambda: self.start_verify(t.repo_id))

    def _kill_external(self, repo_id: str) -> None:
        meter = self.ext_meters.get(repo_id)
        if meter is None or not meter.pid or psutil is None:
            return
        answer = QMessageBox.question(
            self,
            APP_NAME,
            f"Stop the other process (pid {meter.pid}) that is downloading {meter.repo_id}?\n\n"
            "Its partial files are kept until you resume the download here, which drops them "
            "and fetches only the files that are still missing.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        try:
            proc = psutil.Process(meter.pid)
            for child in proc.children(recursive=True):
                child.terminate()
            proc.terminate()
            self.append_line(f"stopped process {meter.pid}; use Resume to pick {meter.repo_id} up here")
        except psutil.Error as exc:
            self.append_line(f"could not stop process {meter.pid}: {exc}")

    def _on_done(self, w: Worker, rc: int) -> None:
        if w in self.workers:
            self.workers.remove(w)
        w.wait(2000)
        w.deleteLater()
        text = {0: "finished", 1: "finished with problems, see the log", 2: "could not start, see the log", 130: "stopped"}
        what = f" ({', '.join(w.repo_ids)})" if w.repo_ids else ""
        self.statusBar().showMessage(text.get(rc, f"finished with exit code {rc}") + what, 15000)
        self.append_line(f"== {text.get(rc, f'exit code {rc}')}{what}")

        ctx = w.ctx
        if ctx is not None and ctx[0] == "download":
            repo_ids, finish_after = ctx[1], ctx[2]
            revision = ctx[3] if len(ctx) > 3 else ""
            if rc != 0:
                for repo_id in repo_ids:  # remembered until it is resumed or forgotten
                    self._remember_interrupted(repo_id, revision)
            if not (finish_after and rc == 0):
                # the download stopped, failed, or was not followed by the finish pass:
                # check what is actually on disk before anyone trusts it
                self.start_verify(repo_ids)
        elif ctx is not None and ctx[0] == "verify":
            for repo_id in ctx[1]:
                self._report_verification(repo_id)
        self._pump_queue()
        self._set_running(bool(self.workers))
        self._save_unfinished()
        self._update_banner()
        if not self.workers:
            self.library.refresh()
            self.browse.refresh_local()

    def _report_verification(self, repo_id: str) -> None:
        row = self.rows.get(repo_id)
        status = self.table.item(row, 0).text() if row is not None else ""
        note = self.table.item(row, 3).text() if row is not None else ""
        if status in ("ready", "complete"):
            self.append_line(f"verified: {repo_id} is complete in the cache ({note}). Finish and move puts it in the library.")
        elif status == "incomplete":
            self.append_line(f"verified: {repo_id} is incomplete, {note}. Click Resume to pick it up (complete files are kept).")
        elif row is None:
            self.append_line(f"verified: nothing of {repo_id} is in the cache.")
        else:
            self.append_line(f"verified: {repo_id} is {status}: {note}")

    def _set_running(self, running: bool) -> None:
        exclusive = self._exclusive_running()
        # downloads may be added while other downloads run, but not during a pass over the whole cache
        for wdg in (self.download_btn, self.resume_btn, self.download_all_btn, self.repo_edit, self.rev_edit):
            wdg.setEnabled(not exclusive)
        # a pass over the whole cache needs the cache to itself
        for wdg in (self.scan_btn, self.run_btn):
            wdg.setEnabled(not self.workers and not self.queue)
        self.stop_btn.setEnabled(running or bool(self.queue))
        self.busy.setVisible(running)
        if not running:
            self.progress_label.setText("")
        n = self._downloads_in_flight()
        if n or self.queue:
            self.transfer_idle.setText(f"{n} download(s) running, {len(self.queue)} queued")
        else:
            self.transfer_idle.setText("No download running.")

    @Slot(int)
    def _tab_changed(self, index: int) -> None:
        if self.tabs.widget(index) is self.library:
            self.library.refresh()

    # -- results

    @Slot(str)
    def append_line(self, line: str) -> None:
        self.log_view.appendPlainText(line)
        if self._log_file is not None:
            try:
                self._log_file.write(line + "\n")
                self._log_file.flush()
            except OSError:
                self._log_file = None

    @Slot()
    def log_view_clear(self) -> None:
        self.log_view.clear()

    # -- the log file

    def _open_log_file(self) -> Path:
        """Every log line also goes to <app data>/hf-downloader.log (rotated at 20 MB)."""
        folder = Path(QStandardPaths.writableLocation(QStandardPaths.StandardLocation.AppLocalDataLocation) or ".")
        path = folder / "hf-downloader.log"
        self._log_file = None
        try:
            folder.mkdir(parents=True, exist_ok=True)
            if path.is_file() and path.stat().st_size > 20 << 20:
                path.replace(path.with_suffix(".log.1"))
            self._log_file = open(path, "a", encoding="utf-8")  # noqa: SIM115 - kept open for the session
            self._log_file.write(f"\n===== {APP_NAME} {APP_VERSION} started {time.strftime('%Y-%m-%d %H:%M:%S')} =====\n")
            self._log_file.flush()
        except OSError:
            self._log_file = None
        return path

    @Slot()
    def open_log(self) -> None:
        if self._log_file is not None:
            try:
                self._log_file.flush()
            except OSError:
                pass
        if not self.log_path.is_file():
            QMessageBox.information(self, APP_NAME, f"No log file yet:\n{self.log_path}")
            return
        if not QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.log_path))):
            QMessageBox.information(self, APP_NAME, f"Could not open the log file; it is at:\n{self.log_path}")

    @Slot(list)
    def _on_repos_found(self, ids: list) -> None:
        for repo_id in ids:
            self._row(repo_id)
            self._set_row(repo_id, "pending", "", None)

    @Slot(str, str, str, object)
    def _on_repo_update(self, repo_id: str, status: str, note: str, size) -> None:
        self._set_row(repo_id, status, note, size)

    def _row(self, repo_id: str) -> int:
        row = self.rows.get(repo_id)
        if row is None:
            row = self.table.rowCount()
            self.table.insertRow(row)
            for col in range(4):
                self.table.setItem(row, col, QTableWidgetItem(""))
            self.table.item(row, 1).setText(repo_id)
            self.rows[repo_id] = row
        return row

    def _set_row(self, repo_id: str, status: str, note: str, size) -> None:
        row = self._row(repo_id)
        item = self.table.item(row, 0)
        item.setText(status)
        color = STATUS_COLORS.get(status)
        if color:
            tint_item(item, QColor(color))
        else:
            item.setForeground(self.palette().text())
            item.setBackground(self.palette().base())
        if size:
            self.table.item(row, 2).setText(hff.human(size))
        if note or status in ("pending", "checking"):
            self.table.item(row, 3).setText(note)
        self.table.scrollToItem(item)

    # -- misc

    def _open_folder(self, path: str) -> None:
        p = Path(path).expanduser()
        if not p.is_dir():
            QMessageBox.information(self, APP_NAME, f"Folder does not exist yet:\n{p}")
            return
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(p)))

    def _about(self) -> None:
        import huggingface_hub
        import PySide6

        QMessageBox.about(
            self,
            "About " + APP_NAME,
            f"<b>{APP_NAME} {APP_VERSION}</b><br>"
            "Download Hugging Face models, finish interrupted downloads and move complete models "
            "out of the hub cache into plain folders.<br><br>"
            f"Engine: hffinish (command line: <code>hffinish</code>)<br>"
            f"huggingface_hub {huggingface_hub.__version__}, PySide6 {PySide6.__version__}, "
            f"psutil {psutil.__version__ if psutil else 'not installed'}, Python {sys.version.split()[0]}",
        )

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802 - Qt API
        if self.workers or self.queue:
            n = len(self.workers)
            answer = QMessageBox.question(
                self,
                APP_NAME,
                f"{n} job(s) are still running" + (f" and {len(self.queue)} queued" if self.queue else "") + ". Stop them and quit?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes:
                event.ignore()
                return
            for item in self.queue:  # running and queued downloads come back as "did not finish"
                self._remember_interrupted(item.repo_id, item.revision)
            self.queue.clear()
            for w in self.workers:
                if w.ctx is not None and w.ctx[0] == "download":
                    for repo_id in w.ctx[1]:
                        self._remember_interrupted(repo_id, w.ctx[3] if len(w.ctx) > 3 else "")
                w.cancel()
            for w in self.workers:
                w.wait(5000)
        if self.library.scanner is not None:
            self.library.scanner.wait(5000)
        if self.browse.worker is not None:
            self.browse.worker.wait(5000)
        if self.browse.local_worker is not None:
            self.browse.local_worker.wait(5000)
        self.stop_external()
        if self.card_dialog is not None:
            self.card_dialog.close()
        self._save_settings()
        if self._log_file is not None:
            try:
                self._log_file.write(f"===== closed {time.strftime('%Y-%m-%d %H:%M:%S')} =====\n")
                self._log_file.close()
            except OSError:
                pass
            self._log_file = None
        event.accept()


# --------------------------------------------------------------------------- theme

# Colours on top of the platform style. Base colours come from the palette
# (palette(...) in the sheet) so the window follows the system's light or
# dark mode; only the accents are fixed.
THEME = f"""
QTabBar::tab {{ padding: 7px 18px; border-bottom: 3px solid transparent; }}
QTabBar::tab:selected {{ border-bottom: 3px solid {ACCENT}; font-weight: bold; }}
QTabBar::tab:hover:!selected {{ border-bottom: 3px solid {ACCENT}80; }}
QGroupBox {{ border: 1px solid palette(mid); border-radius: 6px; margin-top: 12px; padding-top: 6px; }}
QGroupBox::title {{ subcontrol-origin: margin; left: 10px; padding: 0 4px; color: {ACCENT}; font-weight: bold; }}
QProgressBar {{ border: 1px solid palette(mid); border-radius: 4px; text-align: center; height: 20px; }}
QProgressBar::chunk {{ background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 #ffb347, stop:1 {ACCENT}); border-radius: 3px; }}
QPushButton[primary="true"] {{ background: {ACCENT}; color: {contrast_text(QColor(ACCENT)).name()}; font-weight: bold; border: 1px solid #c97a00; border-radius: 4px; padding: 5px 14px; }}
QPushButton[primary="true"]:hover {{ background: #ffb347; }}
QPushButton[primary="true"]:pressed {{ background: #e68d00; }}
QPushButton[primary="true"]:disabled {{ background: palette(mid); color: palette(dark); border-color: palette(mid); }}
QPushButton[danger="true"]:enabled {{ color: #c5221f; font-weight: bold; }}
QHeaderView::section {{ padding: 4px 6px; border: none; border-bottom: 2px solid {ACCENT}; border-right: 1px solid palette(mid); background: palette(button); }}
QTableWidget {{ gridline-color: palette(midlight); selection-background-color: {ACCENT}55; selection-color: palette(text); }}
QStatusBar {{ border-top: 2px solid {ACCENT}; }}
"""


def mark(button: QPushButton, role: str) -> None:
    button.setProperty(role, True)


# --------------------------------------------------------------------------- entry point


def main(argv: list[str]) -> int:
    if WINDOWS:  # own taskbar icon and grouping, also when started through python.exe
        try:
            import ctypes

            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(APP_NAME)
        except (AttributeError, OSError):
            pass
    app = QApplication(argv)
    app.setApplicationName(APP_NAME)
    app.setOrganizationName(APP_NAME)
    app.setApplicationVersion(APP_VERSION)
    icon_path = resource_path("assets/hf-downloader.ico")
    if icon_path.is_file():
        app.setWindowIcon(QIcon(str(icon_path)))
    app.setStyleSheet(THEME)
    pal = app.palette()
    pal.setColor(QPalette.ColorRole.Link, QColor(link_color(pal)))  # Qt's default link blue vanishes on dark
    app.setPalette(pal)
    win = MainWindow()
    win.show()
    if "--selftest" in argv:
        QTimer.singleShot(1500, app.quit)
    else:
        # verify what is in the cache right away: a download that stopped (crash,
        # closed window, lost network) shows up as "incomplete" before anything else
        QTimer.singleShot(300, lambda: win.start_finish(dry_run=True, title="startup check"))
    return app.exec()


if __name__ == "__main__":
    sys.exit(main(sys.argv))
