"""Checks for parse_repo_ref, the repo-id/link parser of the HF-Downloader window.

Run:  .venv\\Scripts\\python tests\\test_parse.py   (needs PySide6, like the window)
"""

import os
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from hf_downloader import (  # noqa: E402
    HfProgress,
    _LineSplitter,
    absolutise,
    hff,
    parse_hf_progress,
    parse_repo_list,
    parse_repo_ref,
    parse_size,
    restart_due,
    split_front_matter,
)


class _Recorder:
    def __init__(self) -> None:
        self.items: list[str] = []

    def emit(self, text: str) -> None:
        self.items.append(text)


class _StubWorker:
    """Stands in for Worker under _LineSplitter: records log lines, progress text and parsed counters."""

    def __init__(self) -> None:
        self.line = _Recorder()
        self.progress = _Recorder()
        self.counters = HfProgress()
        self.seen: list[str] = []

    def note_progress(self, segment: str) -> bool:
        if parse_hf_progress(segment, self.counters, 1.0):
            self.seen.append(segment)
            return True
        return False


# the byte stream of `hf download --format human` as it arrives through the pipe, in uneven chunks
STREAM_CHUNKS = [
    b"\r\n\rDownloading bytes:           |  0.00B            \x1b[A\r\n\rReconstructing (incomplete total...): |          |  0.00B /  0.00B            \x1b",
    b"[A\r\n\rFetching 5 files:   0%|          | 0/5 [00:00<?, ?it/s]\x1b[A",
    b"C:\\x\\file_download.py:149: UserWarning: symlinks\r\n  warnings.warn(message)\r",  # "\r\n" split across chunks
    b"\n",
    b"\rDownloading bytes: \xe2\x96\x88\xe2\x96\x88\xe2\x96\x88\xe2\x96\x8c      | 6.31MB, 2.13MB/s  \x1b[A\r\n",
    b"\rReconstruction complete: 100%|\xe2\x96\x88\xe2\x96\x88| 18.0MB / 18.0MB, 2.89MB/s               \x1b[A\xe2\x9c\x93 Downloaded\r\n  path: C:\\x\\snapshots\\abc\r\n",
]
STREAM_WANT_LINES = ["C:\\x\\file_download.py:149: UserWarning: symlinks", "  warnings.warn(message)", "\u2713 Downloaded", "  path: C:\\x\\snapshots\\abc"]

CASES = {
    "convaiinnovations/laya": ("convaiinnovations/laya", ""),
    "  convaiinnovations/laya/  ": ("convaiinnovations/laya", ""),
    "convaiinnovations/laya.git": ("convaiinnovations/laya", ""),
    "hf download convaiinnovations/laya": ("convaiinnovations/laya", ""),
    "hf download convaiinnovations/laya --revision v2 --include *.safetensors": ("convaiinnovations/laya", "v2"),
    "hf download --revision=abc convaiinnovations/laya": ("convaiinnovations/laya", "abc"),
    "huggingface-cli download convaiinnovations/laya": ("convaiinnovations/laya", ""),
    "https://huggingface.co/convaiinnovations/laya": ("convaiinnovations/laya", ""),
    "https://huggingface.co/convaiinnovations/laya?clone=true": ("convaiinnovations/laya", ""),
    "https://huggingface.co/convaiinnovations/laya/": ("convaiinnovations/laya", ""),
    "https://huggingface.co/convaiinnovations/laya/tree/main": ("convaiinnovations/laya", "main"),
    "https://huggingface.co/convaiinnovations/laya/tree/refs%2Fpr%2F1": ("convaiinnovations/laya", "refs%2Fpr%2F1"),
    "https://huggingface.co/convaiinnovations/laya/blob/main/config.json": ("convaiinnovations/laya", "main"),
    "https://huggingface.co/convaiinnovations/laya/resolve/abc123/model.safetensors": ("convaiinnovations/laya", "abc123"),
    "https://huggingface.co/models/convaiinnovations/laya": ("convaiinnovations/laya", ""),
    "huggingface.co/convaiinnovations/laya": ("convaiinnovations/laya", ""),
    "hf.co/convaiinnovations/laya": ("convaiinnovations/laya", ""),
    "hf://convaiinnovations/laya": ("convaiinnovations/laya", ""),
    "git clone https://huggingface.co/convaiinnovations/laya": ("convaiinnovations/laya", ""),
    "git clone https://huggingface.co/convaiinnovations/laya.git": ("convaiinnovations/laya", ""),
    "git lfs clone 'https://huggingface.co/convaiinnovations/laya'": ("convaiinnovations/laya", ""),
    '"https://huggingface.co/convaiinnovations/laya?clone=true"': ("convaiinnovations/laya", ""),
    "Qwen/Qwen2.5-0.5B-Instruct": ("Qwen/Qwen2.5-0.5B-Instruct", ""),
}

REJECTED = [
    "",
    "laya",
    "https://github.com/org/repo",
    "https://huggingface.co/datasets/org/name",
    "hf download --revision main",
    "org/na me",
]


LIST_TEXT = """
# a comment line
convaiinnovations/laya
hf download Qwen/Qwen2.5-0.5B-Instruct --revision v2
https://huggingface.co/convaiinnovations/laya?clone=true,
not a repo
ConvaiInnovations/LAYA/tree/v3

git clone https://huggingface.co/org/name.git
"""
LIST_WANT = ([("convaiinnovations/laya", ""), ("Qwen/Qwen2.5-0.5B-Instruct", "v2"), ("org/name", "")], ["not a repo"])

# what `hf download --format human` prints through a pipe (captured from huggingface_hub 2.0.0)
PROGRESS_LINES = [
    "\rDownloading bytes:           |  0.00B            ",
    "\x1b[A\rReconstructing (incomplete total...):   0%|          |  0.00B / 17.8MB            ",
    "Fetching 5 files:  20%|██        | 1/5 [00:00<00:01,  2.42it/s]",
    "Downloading bytes: ███▌      | 6.31MB, 2.13MB/s  \x1b[A",
    "Reconstructing (incomplete total...):  35%|███▌      |  6.31MB / 17.8MB, 2.13MB/s",
    "Download complete: ██████████| 16.9MB, 2.58MB/s  ",
    "Reconstruction complete: 100%|██████████| 18.0MB / 18.0MB, 2.89MB/s  ",
]
PROGRESS_WANT = dict(received=16_900_000, rate=2_580_000, written=18_000_000, total=18_000_000, files_done=1, files=5)
NOT_PROGRESS = [
    "  warnings.warn(message)",
    "path=C:\\Users\\x\\.cache\\huggingface\\hub\\models--a--b\\snapshots\\abc",
    "✓ Downloaded",
    "Downloading something else entirely",
]

CARD = """---
license: apache-2.0
tags:
- text-generation
- gguf
base_model: org/base
---
# Title

![hero](assets/hero.png) and <img src="./pic.jpg" width="200"> and ![abs](https://x.org/a.png)
See [the config](config.json) and [site](https://example.com).
"""
CARD_WANT_META = {"license": "apache-2.0", "tags": "text-generation, gguf", "base_model": "org/base"}
CARD_WANT_IMAGES = [
    "https://huggingface.co/org/name/resolve/main/assets/hero.png",
    "https://huggingface.co/org/name/resolve/main/pic.jpg",
    "https://x.org/a.png",
]


def extra_checks() -> int:
    failures = 0
    got = parse_repo_list(LIST_TEXT)
    want = (LIST_WANT[0], LIST_WANT[1])
    if got[0] != want[0] or [line for line, _why in got[1]] != want[1]:
        failures += 1
        print(f"FAIL parse_repo_list: got {got!r}")
    meta, body = split_front_matter(CARD)
    if meta != CARD_WANT_META or not body.lstrip().startswith("# Title"):
        failures += 1
        print(f"FAIL split_front_matter: got {meta!r}, body starts {body[:20]!r}")
    text, images = absolutise(body, "org/name")
    if sorted(images) != sorted(CARD_WANT_IMAGES):
        failures += 1
        print(f"FAIL absolutise images: got {images!r}")
    if "(https://huggingface.co/org/name/blob/main/config.json)" not in text or "(https://example.com)" not in text:
        failures += 1
        print(f"FAIL absolutise links: {text!r}")
    p = HfProgress()
    for i, line in enumerate(PROGRESS_LINES):
        if not parse_hf_progress(line, p, float(i + 1)):
            failures += 1
            print(f"FAIL parse_hf_progress did not recognise {line!r}")
    got = {k: getattr(p, k) for k in PROGRESS_WANT}
    if got != PROGRESS_WANT or p.received_t != 6.0 or p.seen_t != 7.0:
        failures += 1
        print(f"FAIL parse_hf_progress: got {got!r} received_t={p.received_t} seen_t={p.seen_t}")
    for line in NOT_PROGRESS:
        if parse_hf_progress(line, p, 9.0):
            failures += 1
            print(f"FAIL parse_hf_progress took {line!r} for a progress line")
    if parse_size("1.5", "GiB") != 1610612736 or parse_size("456", "kB") != 456000 or parse_size("0.00", "B") != 0:
        failures += 1
        print("FAIL parse_size")
    stub = _StubWorker()
    splitter = _LineSplitter(stub)
    decoder = __import__("codecs").getincrementaldecoder("utf-8")("replace")
    for chunk in STREAM_CHUNKS:
        splitter.write(decoder.decode(chunk))
    splitter.flush()
    if stub.line.items != STREAM_WANT_LINES:
        failures += 1
        print(f"FAIL _LineSplitter lines: got {stub.line.items!r}")
    if len(stub.seen) != 5 or stub.counters.received != 6_310_000 or stub.counters.written != 18_000_000 or stub.counters.files != 5:
        failures += 1
        print(f"FAIL _LineSplitter counters: {len(stub.seen)} progress segments, {stub.counters!r}")
    # A finished log line clears the status text (""). A progress redraw must not.
    shown = [item for item in stub.progress.items if item]
    if shown:
        failures += 1
        print(f"FAIL _LineSplitter progress text: got {stub.progress.items!r}, progress lines must not reach the status bar")
    # 90 s of silence used to restart a download and delete its partial files.
    # Xet is often quiet for longer than that between 64 MB writes.
    if restart_due(90) or restart_due(599) or not restart_due(600) or restart_due(10_000, stopped=True):
        failures += 1
        print(f"FAIL restart_due: 90->{restart_due(90)} 599->{restart_due(599)} 600->{restart_due(600)}")
    if (
        not hff.argv_is_checker(["C:/Tools/hffinish", "--dry-run"])
        or hff.argv_is_checker(["python", "hffinish", "--snapshot-download", "org/name"])
        or hff.argv_is_checker(["hf", "download", "org/name"])
        or hff.argv_is_checker(["fakedl", "hf-internal-testing/tiny-random-gpt2"])
    ):
        failures += 1
        print("FAIL argv_is_checker")
    try:
        from huggingface_hub.utils import get_session

        timeout = get_session().timeout
        read = getattr(timeout, "read", None)
        if read is None or float(read) < 120:
            failures += 1
            print(f"FAIL hub timeout: {timeout!r}")
    except Exception as exc:  # noqa: BLE001 - the check reports the cause
        failures += 1
        print(f"FAIL hub timeout: {type(exc).__name__}: {exc}")
    return failures


def main() -> int:
    failures = 0
    for text, want in CASES.items():
        try:
            got = parse_repo_ref(text)
        except ValueError as exc:
            got = f"ValueError: {exc}"
        if got != want:
            failures += 1
            print(f"FAIL {text!r}: got {got!r}, want {want!r}")
    for text in REJECTED:
        try:
            got = parse_repo_ref(text)
        except ValueError:
            continue
        failures += 1
        print(f"FAIL {text!r}: accepted as {got!r}, should be rejected")
    failures += extra_checks()
    total = len(CASES) + len(REJECTED) + 4 + len(PROGRESS_LINES) + 1 + len(NOT_PROGRESS) + 1 + 3 + 3
    print(f"{total - failures}/{total} passed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
