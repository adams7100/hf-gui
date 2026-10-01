"""Checks for parse_repo_ref, the repo-id/link parser of the HF-Downloader window.

Run:  .venv\\Scripts\\python tests\\test_parse.py   (needs PySide6, like the window)
"""

import os
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from hf_downloader import absolutise, parse_repo_list, parse_repo_ref, split_front_matter  # noqa: E402

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

git clone https://huggingface.co/org/name.git
"""
LIST_WANT = ([("convaiinnovations/laya", ""), ("Qwen/Qwen2.5-0.5B-Instruct", "v2"), ("org/name", "")], ["not a repo"])

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
    total = len(CASES) + len(REJECTED) + 4
    print(f"{total - failures}/{total} passed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
