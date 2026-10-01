#!/usr/bin/env -S uv run --quiet --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["huggingface_hub>=1.32", "PySide6>=6.6", "psutil>=5.9"]
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
import contextlib
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

from PySide6.QtCore import QSettings, Qt, QThread, QTimer, QUrl, Signal, Slot
from PySide6.QtGui import QAction, QCloseEvent, QColor, QDesktopServices, QFont, QFontDatabase, QIcon, QImage
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

    def __init__(self, emit, label: str, repo_id: str, cache_dir: Path, expected: int) -> None:
        super().__init__(daemon=True, name="transfer-meter")
        self.emit = emit
        self.label = label
        self.repo_dir = cache_dir / f"models--{repo_id.replace('/', '--')}"
        self.expected = expected
        self._stop = threading.Event()

    def stop(self) -> None:
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
        net = self._net()
        speed = 0.0
        self.emit(Transfer(self.label, last, self.expected, 0.0, -1.0 if net is None else 0.0, -1.0 if net is None else 0.0))
        while not self._stop.wait(1.0):
            now = time.monotonic()
            cur = self.measure()
            dt = max(now - last_t, 1e-3)
            inst = max(cur - last, 0) / dt
            speed = inst if speed == 0 else 0.7 * speed + 0.3 * inst
            down = up = -1.0
            net2 = self._net()
            if net is not None and net2 is not None:
                down = max(net2.bytes_recv - net.bytes_recv, 0) / dt
                up = max(net2.bytes_sent - net.bytes_sent, 0) / dt
            net = net2
            last_t, last = now, cur
            self.emit(Transfer(self.label, cur, self.expected, speed, down, up))
        self.emit(Transfer(self.label, self.measure(), self.expected, 0.0, -1.0, -1.0, final=True))


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


class Worker(QThread):
    """Runs one job (a callable taking the worker) off the GUI thread.

    While it runs, hffinish's `log`, `run_download` and `process` are replaced
    so every message, every byte of download output and every status change
    arrives here as a signal.
    """

    line = Signal(str)  # a finished log line
    progress = Signal(str)  # transient progress text ("" clears it)
    repos_found = Signal(list)  # repo ids the job is about to work on
    repo_update = Signal(str, str, str, object)  # repo id, status, note, total bytes or None
    transfer = Signal(object)  # a Transfer reading of the running download
    done = Signal(int)  # exit code

    def __init__(self, job, cache_dir: Path, parent=None) -> None:
        super().__init__(parent)
        self.job = job
        self.cache_dir = cache_dir
        self.batch_label = ""  # "[2/5] " while a list is being downloaded
        self._cancel = threading.Event()
        self._proc: subprocess.Popen | None = None
        self._lock = threading.Lock()
        self._meter: TransferMeter | None = None

    # -- control

    def cancel(self) -> None:
        self._cancel.set()
        with self._lock:
            proc = self._proc
        if proc is not None and proc.poll() is None:
            try:
                proc.terminate()
            except OSError:
                pass

    @property
    def cancelled(self) -> bool:
        return self._cancel.is_set()

    # -- progress meter

    def start_meter(self, repo_id: str, expected: int) -> None:
        self.stop_meter()
        self._meter = TransferMeter(self.transfer.emit, self.batch_label + repo_id, repo_id, self.cache_dir, expected)
        self._meter.start()

    def stop_meter(self) -> None:
        if self._meter is not None:
            self._meter.stop()
            self._meter.join(3)
            self._meter = None

    # -- thread body

    def run(self) -> None:
        sink = _LineSplitter(self)
        saved = (hff.log, hff.run_download, hff.process)
        self._orig_process = hff.process
        hff.log, hff.run_download, hff.process = self._log, self._run_download, self._process
        rc = 1
        try:
            with contextlib.redirect_stdout(sink), contextlib.redirect_stderr(sink):
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
            hff.log, hff.run_download, hff.process = saved
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

    def _process(self, repo, *args) -> None:
        self.repo_update.emit(repo.repo_id, "checking", "", None)
        try:
            self._orig_process(repo, *args)
        finally:
            self.repo_update.emit(repo.repo_id, repo.status or "checked", repo.note, repo.total_bytes or None)

    def _run_download(self, repo, cmd: list[str], env: dict[str, str]) -> int:
        if repo is not None:
            self.repo_update.emit(repo.repo_id, "downloading", os.path.basename(cmd[0]), repo.total_bytes or None)
            self.start_meter(repo.repo_id, repo.total_bytes)
        try:
            return self.stream(cmd, env)
        finally:
            self.stop_meter()

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


def _download_one(w: Worker, repo_id: str, revision: str, opts: Options, finish_after: bool) -> int:
    """Download one repo into the cache (with the meter running), then optionally finish it."""
    w.repo_update.emit(repo_id, "downloading", "asking the Hub for the file list", None)
    expected = expected_size(repo_id, revision)
    if expected:
        w.line.emit(f"{repo_id}: {hff.human(expected)} at {revision or 'main'}")
    w.repo_update.emit(repo_id, "downloading", "", expected or None)
    env = hff.tool_env()
    hf = shutil.which("hf", path=env["PATH"]) or shutil.which("hf")
    w.start_meter(repo_id, expected)
    try:
        if hf:
            cmd = [hf, "download", repo_id]
            if revision:
                cmd += ["--revision", revision]
            if opts.cache_dir:
                cmd += ["--cache-dir", opts.cache_dir]
            w.line.emit("running: " + " ".join(cmd))
            rc = w.stream(cmd, env)
        else:
            from huggingface_hub import snapshot_download

            w.line.emit("hf CLI not found on PATH, downloading in-process (cannot be stopped midway)")
            try:
                snapshot_download(repo_id, revision=revision or None, cache_dir=opts.cache_dir or None)
                rc = 0
            except Exception as exc:  # noqa: BLE001
                w.line.emit(f"download failed: {exc}")
                rc = 1
    finally:
        w.stop_meter()
    if rc != 0:
        w.repo_update.emit(repo_id, "error", f"download exited with code {rc}", None)
        return rc
    w.repo_update.emit(repo_id, "downloaded", "in the cache", None)
    if not finish_after:
        return 0
    w.line.emit("")
    return hff.main(opts.argv(filters=[repo_id]))


def download_job(repo_id: str, revision: str, opts: Options, finish_after: bool):
    """Download one repo into the cache, then optionally run the finish pass on it."""

    def job(w: Worker) -> int:
        w.repos_found.emit([repo_id])
        return _download_one(w, repo_id, revision, opts, finish_after)

    return job


def batch_job(items: list[tuple[str, str]], opts: Options, finish_after: bool):
    """Download a pasted list one repo after the other; exit 1 if any of them failed."""

    def job(w: Worker) -> int:
        w.repos_found.emit([repo_id for repo_id, _rev in items])
        failed: list[str] = []
        for n, (repo_id, revision) in enumerate(items, 1):
            if w.cancelled:
                raise KeyboardInterrupt
            w.batch_label = f"[{n}/{len(items)}] "
            w.line.emit(f"== [{n}/{len(items)}] {repo_id}" + (f" @ {revision}" if revision else ""))
            rc = _download_one(w, repo_id, revision, opts, finish_after)
            if rc != 0:
                failed.append(repo_id)
            w.line.emit("")
        w.batch_label = ""
        if failed:
            w.line.emit(f"{len(failed)} of {len(items)} download(s) did not finish: " + ", ".join(failed))
            return 1
        w.line.emit(f"all {len(items)} download(s) finished")
        return 0

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
        self.where = QLabel()
        self.where.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
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
        top.addWidget(QLabel("Library:"))
        top.addWidget(self.where, 1)
        top.addWidget(self.verify_btn)
        top.addWidget(self.resume_btn)
        top.addWidget(self.open_model_btn)
        top.addWidget(self.open_btn)
        top.addWidget(self.refresh_btn)
        root.addLayout(top)

        self.table = QTableWidget(0, 7)
        self.table.setHorizontalHeaderLabels(["Model", "State", "Files", "Size", "Last changed", "Details", "Folder"])
        header = self.table.horizontalHeader()
        header.setMinimumSectionSize(70)
        for col in range(5):
            header.setSectionResizeMode(col, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(5, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(6, QHeaderView.ResizeMode.Stretch)
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
    def refresh(self) -> None:
        if self.scanner is not None:
            return
        opts = self.window._options()
        self.where.setText(str(opts.dest_path()))
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
                e.state,
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
                if col in (2, 3):
                    item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
                if col == 1:
                    color = LIBRARY_COLORS.get(e.state)
                    if color:
                        tint_item(item, QColor(color))
                self.table.setItem(row, col, item)
        self.table.setSortingEnabled(True)
        self._selection_changed()
        ready = [e for e in entries if e.state == "ready"]
        cached = [e for e in entries if e.in_cache]
        incomplete = [e for e in entries if e.state == "incomplete"]
        downloading = [e for e in entries if e.state == "downloading"]
        text = f"{len(ready)} model(s) in the library, {hff.human(sum(e.size for e in ready))}"
        if cached:
            text += f"; {len(cached)} in the hub cache"
            if downloading:
                text += f", {len(downloading)} being downloaded by another process"
            if incomplete:
                text += f", {len(incomplete)} incomplete (select it and click Resume download)"
        self.summary.setText(text)

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
            self.window.show_download_tab()
            self.window.start_resume(e.repo_id)


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


def tint_item(item: QTableWidgetItem, color: QColor) -> None:
    """Coloured text on a light wash of the same colour; readable on light and dark themes."""
    wash = QColor(color)
    wash.setAlpha(48)
    item.setBackground(wash)
    item.setForeground(color)


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

        legend = QHBoxLayout()
        legend.addWidget(QLabel("Categories:"))
        for name, color in CATEGORY_COLORS.items():
            chip = QLabel(f"<span style='color:{color}'>&#9632;</span> {name}")
            chip.setToolTip(f"{name} models: the Task column is coloured like this")
            legend.addWidget(chip)
        legend.addSpacing(24)
        legend.addWidget(QLabel("Downloaded:"))
        for name, color in LOCAL_COLORS.items():
            if name == "unverified":
                continue
            chip = QLabel(f"<span style='color:{color}'>&#9632;</span> {name}")
            legend.addWidget(chip)
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
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setAlternatingRowColors(True)
        self.table.setSortingEnabled(True)
        self.table.doubleClicked.connect(lambda _idx: self._details())
        self.table.itemSelectionChanged.connect(self._selection_changed)
        root.addWidget(self.table, 1)

        actions = QHBoxLayout()
        self.status = QLabel("Nothing scraped yet.")
        self.details_btn = QPushButton("View details")
        self.details_btn.setToolTip("The whole model card with its images, plus the file list")
        self.details_btn.clicked.connect(self._details)
        self.download_btn = QPushButton("Download")
        self.download_btn.clicked.connect(self._download)
        self.add_btn = QPushButton("Add to list")
        self.add_btn.setToolTip("Append it to the list on the Download tab")
        self.add_btn.clicked.connect(self._add)
        self.open_btn = QPushButton("Open on huggingface.co")
        self.open_btn.clicked.connect(self._open)
        actions.addWidget(self.status, 1)
        actions.addWidget(self.details_btn)
        actions.addWidget(self.download_btn)
        actions.addWidget(self.add_btn)
        actions.addWidget(self.open_btn)
        root.addLayout(actions)
        self._selection_changed()
        self.next_btn.setEnabled(False)

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
        self._selection_changed()

    @staticmethod
    def _paint_local(item: QTableWidgetItem, local: LocalState) -> None:
        item.setText(local.state)
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

    # -- selection

    def selected(self) -> HubModel | None:
        rows = self.table.selectionModel().selectedRows()
        if not rows:
            return None
        return self.table.item(rows[0].row(), 0).data(Qt.ItemDataRole.UserRole)

    @Slot()
    def _selection_changed(self) -> None:
        on = self.selected() is not None
        for btn in (self.details_btn, self.download_btn, self.add_btn, self.open_btn):
            btn.setEnabled(on)

    def _details(self) -> None:
        m = self.selected()
        if m is not None:
            self.window.show_details(m.repo_id)

    def _download(self) -> None:
        m = self.selected()
        if m is not None:
            self.window.download_repo(m.repo_id)

    def _add(self) -> None:
        m = self.selected()
        if m is not None:
            self.window.add_to_list(m.repo_id)

    def _open(self) -> None:
        m = self.selected()
        if m is not None:
            QDesktopServices.openUrl(QUrl(m.url))


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
        self.view = CardView()
        split = QSplitter(Qt.Orientation.Vertical)
        split.addWidget(self.files)
        split.addWidget(self.view)
        split.setStretchFactor(0, 1)
        split.setStretchFactor(1, 4)
        root.addWidget(split, 1)

    def load(self, repo_id: str) -> None:
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
            parts.append(f"<b style='color:{color}'>{local.state}</b>: {local.note}")
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
        self.view.setMarkdown(card.markdown or "*No model card text.*")

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802 - Qt API
        if self.worker is not None:
            self.worker.ready.disconnect(self._ready)
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


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle(APP_NAME)
        self.resize(1040, 760)
        self.settings = QSettings(APP_NAME, APP_NAME)
        self.worker: Worker | None = None
        self.rows: dict[str, int] = {}
        self._download_ctx: tuple[list[str], bool] | None = None  # (repo ids, finish after) of the running download
        self._verify_ctx: list[str] | None = None  # repo ids a verification pass is about
        self.card_dialog: ModelCardDialog | None = None
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
        list_btns.addWidget(self.download_all_btn)
        list_btns.addWidget(self.clear_list_btn)
        list_btns.addWidget(self.list_count)
        list_btns.addStretch(1)
        list_row.addWidget(self.list_edit, 1)
        list_row.addLayout(list_btns)
        dl_box.addLayout(list_row)
        root.addWidget(dl)

        # finish cached models
        fin = QGroupBox("Finish and move cached models")
        form = QFormLayout(fin)
        self.cache_edit = QLineEdit()
        self.cache_edit.setPlaceholderText(str(hff.constants.HF_HUB_CACHE))
        form.addRow("Hub cache:", self._path_row(self.cache_edit, "Choose the Hugging Face hub cache"))
        self.dest_edit = QLineEdit()
        self.dest_edit.setPlaceholderText(str(hff.DEFAULT_DEST))
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
        self.clear_btn.clicked.connect(self.log_view_clear)
        buttons.addWidget(self.scan_btn)
        buttons.addWidget(self.run_btn)
        buttons.addWidget(self.stop_btn)
        buttons.addStretch(1)
        buttons.addWidget(self.clear_btn)
        form.addRow(buttons)
        root.addWidget(fin)

        # progress of the running download
        prog = QHBoxLayout()
        self.xfer_bar = QProgressBar()
        self.xfer_bar.setRange(0, 1000)
        self.xfer_bar.setValue(0)
        self.xfer_bar.setFormat("%p%")
        self.xfer_bar.setMinimumWidth(220)
        self.xfer_label = QLabel("No download running.")
        self.xfer_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        prog.addWidget(self.xfer_bar, 1)
        prog.addWidget(self.xfer_label, 2)
        root.addLayout(prog)

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
            start = edit.text().strip() or edit.placeholderText()
            chosen = QFileDialog.getExistingDirectory(self, title, start)
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
        """Download one repo (from the field, the Hub browser or the model card window)."""
        if self.worker is not None:
            QMessageBox.information(self, APP_NAME, "A job is already running. Stop it first, or add the model to the list.")
            return
        local = self.local_state(repo_id)
        if local.downloaded and not self._confirm_redownload(repo_id, local):
            return
        self.show_download_tab()
        self.repo_edit.setText(repo_id)
        self.rev_edit.setText(revision)
        opts = self._options()
        finish_after = self.finish_after_cb.isChecked()
        detail = f"hf download {repo_id}" + (f" --revision {revision}" if revision else "")
        if finish_after:
            detail += ", then hffinish " + " ".join(opts.argv(filters=[repo_id]))
        self._download_ctx = ([repo_id], finish_after)
        self._start(download_job(repo_id, revision, opts, finish_after), detail)

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
        if self.worker is not None:
            QMessageBox.information(self, APP_NAME, "A job is already running. Stop it first.")
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
            box.setText(f"{len(already)} of the {len(items)} repo(s) in the list have already been downloaded:\n\n{shown}")
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
                    self.statusBar().showMessage("Everything in the list is already downloaded.", 6000)
                    return
            elif clicked is not again:
                return
        finish_after = self.finish_after_cb.isChecked()
        self._download_ctx = ([repo_id for repo_id, _rev in items], finish_after)
        title = f"downloading {len(items)} repo(s) from the list" + (", each verified and moved" if finish_after else "")
        self._start(batch_job(items, opts, finish_after), title)

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

    def start_finish(self, dry_run: bool, title: str = "") -> None:
        opts = self._options(dry_run=dry_run)
        self._start(finish_job(opts), (title + ": " if title else "") + "hffinish " + " ".join(opts.argv()))

    def start_verify(self, repo_ids: str | list[str]) -> None:
        """Check repos in the cache file by file (a dry run limited to them)."""
        ids = [repo_ids] if isinstance(repo_ids, str) else list(repo_ids)
        opts = self._options(dry_run=True)
        opts.filters = ids
        self._verify_ctx = ids
        self._start(finish_job(opts), f"verifying {', '.join(ids)}: hffinish " + " ".join(opts.argv()))

    def start_resume(self, repo_id: str, revision: str = "") -> None:
        """Pick a download up where it stopped.

        For a repo that is in the cache this is a finish pass limited to it:
        complete files are kept, stale partial files are dropped (huggingface_hub
        never appends to them), the rest is downloaded again at the cached
        commit, then the model is verified and moved. A repo that is not in the
        cache yet is simply downloaded.
        """
        if self.worker is not None:
            QMessageBox.information(self, APP_NAME, "A job is already running. Stop it first.")
            return
        opts = self._options()
        repo_dir = opts.cache_path() / f"models--{repo_id.replace('/', '--')}"
        if not (repo_dir / "snapshots").is_dir():
            self.append_line(f"{repo_id} is not in the cache yet, starting the download")
            self.download_repo(repo_id, revision)
            return
        self.show_download_tab()
        self.repo_edit.setText(repo_id)
        opts.filters = [repo_id]
        opts.no_download = False
        opts.dry_run = False
        self._download_ctx = ([repo_id], not opts.no_move)
        self._start(finish_job(opts), f"resuming {repo_id}: hffinish " + " ".join(opts.argv()))

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
    def stop(self) -> None:
        if self.worker is not None:
            self.append_line("stopping...")
            self.worker.cancel()

    def _start(self, job, title: str) -> None:
        if self.worker is not None:
            self.statusBar().showMessage("A job is already running; stop it first.", 5000)
            return
        self._save_settings()
        self.table.setRowCount(0)
        self.rows.clear()
        self.append_line(f"== {title}")
        w = Worker(job, self._options().cache_path(), self)
        w.line.connect(self.append_line)
        w.progress.connect(self.progress_label.setText)
        w.repos_found.connect(self._on_repos_found)
        w.repo_update.connect(self._on_repo_update)
        w.transfer.connect(self._on_transfer)
        w.done.connect(self._on_done)
        self.worker = w
        self._set_running(True)
        w.start()

    @Slot(object)
    def _on_transfer(self, t: Transfer) -> None:
        if t.total > 0:
            self.xfer_bar.setRange(0, 1000)
            self.xfer_bar.setValue(max(0, min(1000, int(1000 * t.done / t.total))))
            self.xfer_bar.setFormat(f"%p%  {hff.human(t.done)} / {hff.human(t.total)}")
        else:
            self.xfer_bar.setRange(0, 0)  # size unknown: busy bar
            self.xfer_bar.setFormat(hff.human(t.done))
        if t.final:
            self.xfer_label.setText(f"{t.label}: finished, {hff.human(t.done)} in the cache")
            self.xfer_bar.setRange(0, 1000)
            self.xfer_bar.setValue(1000 if t.total and t.done >= t.total else self.xfer_bar.value())
            return
        bits = [f"{t.label}: {rate(t.speed)}"]
        if t.total > 0 and t.speed > 0 and t.done < t.total:
            secs = int((t.total - t.done) / t.speed)
            eta = f"{secs // 3600}:{secs % 3600 // 60:02d}:{secs % 60:02d}" if secs >= 3600 else f"{secs // 60}:{secs % 60:02d}"
            bits.append(f"ETA {eta}")
        if t.down >= 0:
            bits.append(f"network down {rate(t.down)}, up {rate(t.up)}")
        self.xfer_label.setText("   ".join(bits))

    @Slot(int)
    def _on_done(self, rc: int) -> None:
        w = self.worker
        self.worker = None
        if w is not None:
            w.wait(2000)
            w.deleteLater()
        self._set_running(False)
        text = {0: "finished", 1: "finished with problems, see the log", 2: "could not start, see the log", 130: "stopped"}
        self.statusBar().showMessage(text.get(rc, f"finished with exit code {rc}"), 15000)
        self.append_line(f"== {text.get(rc, f'exit code {rc}')}")

        download_ctx, self._download_ctx = self._download_ctx, None
        verify_ctx, self._verify_ctx = self._verify_ctx, None
        if download_ctx is not None:
            repo_ids, finish_after = download_ctx
            if not (finish_after and rc == 0):
                # the download stopped, failed, or was not followed by the finish pass:
                # check what is actually on disk before anyone trusts it
                QTimer.singleShot(0, lambda: self.start_verify(repo_ids))
                return
        elif verify_ctx is not None:
            for repo_id in verify_ctx:
                self._report_verification(repo_id)
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
        for wdg in (
            self.download_btn,
            self.resume_btn,
            self.download_all_btn,
            self.scan_btn,
            self.run_btn,
            self.repo_edit,
            self.rev_edit,
        ):
            wdg.setEnabled(not running)
        self.stop_btn.setEnabled(running)
        self.busy.setVisible(running)
        if running:
            self.xfer_bar.setRange(0, 1000)
            self.xfer_bar.setValue(0)
            self.xfer_bar.setFormat("%p%")
            self.xfer_label.setText("Waiting for a download to start...")
        else:
            self.progress_label.setText("")
            if self.xfer_bar.maximum() == 0:
                self.xfer_bar.setRange(0, 1000)
            if self.xfer_label.text().startswith("Waiting"):
                self.xfer_label.setText("No download running.")

    @Slot(int)
    def _tab_changed(self, index: int) -> None:
        if self.tabs.widget(index) is self.library:
            self.library.refresh()

    # -- results

    @Slot(str)
    def append_line(self, line: str) -> None:
        self.log_view.appendPlainText(line)

    @Slot()
    def log_view_clear(self) -> None:
        self.log_view.clear()

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
        if self.worker is not None:
            answer = QMessageBox.question(
                self,
                APP_NAME,
                "A job is still running. Stop it and quit?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes:
                event.ignore()
                return
            self.worker.cancel()
            self.worker.wait(5000)
        if self.library.scanner is not None:
            self.library.scanner.wait(5000)
        if self.browse.worker is not None:
            self.browse.worker.wait(5000)
        if self.browse.local_worker is not None:
            self.browse.local_worker.wait(5000)
        if self.card_dialog is not None:
            self.card_dialog.close()
        self._save_settings()
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
QPushButton[primary="true"] {{ background: {ACCENT}; color: #1f1300; font-weight: bold; border: 1px solid #c97a00; border-radius: 4px; padding: 5px 14px; }}
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
