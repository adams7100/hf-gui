"""HF-Downloader downloads models and never uploads anything.

This check reads the two sources (hffinish and hf_downloader.py) and fails if
any Hub write API, HTTP write method or ``hf`` subcommand other than
``download`` ever appears in them, and if the telemetry opt-out is missing.

Run:  .venv\\Scripts\\python tests\\test_no_upload.py
"""

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCES = [ROOT / "hffinish", ROOT / "hf_downloader.py"]

# huggingface_hub write APIs (HfApi methods and module-level helpers)
WRITE_APIS = [
    "upload_file",
    "upload_folder",
    "upload_large_folder",
    "create_commit",
    "create_repo",
    "delete_repo",
    "delete_file",
    "delete_folder",
    "create_branch",
    "create_tag",
    "create_pull_request",
    "create_discussion",
    "comment_discussion",
    "update_repo_settings",
    "update_repo_visibility",
    "move_repo",
    "push_to_hub",
    "preupload_lfs_files",
    "CommitOperationAdd",
    "CommitOperationDelete",
    "CommitOperationCopy",
    "send_telemetry",
    "like(",
    "unlike(",
    "add_collection_item",
    "create_collection",
    "create_webhook",
]

# HTTP methods that send data
HTTP_WRITES = re.compile(r"\.(post|put|patch|delete)\(|method\s*=\s*['\"](POST|PUT|PATCH|DELETE)['\"]")

# every `hf` subcommand the sources start as a child process must be `download`
HF_SUBCOMMANDS = re.compile(r"\[\s*hf\s*,\s*\"([a-z-]+)\"")


def main() -> int:
    failures = 0
    for path in SOURCES:
        text = path.read_text(encoding="utf-8")
        for name in WRITE_APIS:
            if name in text:
                failures += 1
                print(f"FAIL {path.name}: uses {name!r}, a Hub write API")
        for m in HTTP_WRITES.finditer(text):
            failures += 1
            print(f"FAIL {path.name}: HTTP write at offset {m.start()}: {m.group(0)!r}")
        for m in HF_SUBCOMMANDS.finditer(text):
            if m.group(1) != "download":
                failures += 1
                print(f"FAIL {path.name}: starts `hf {m.group(1)}`, only `hf download` is allowed")
        if 'os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")' not in text:
            failures += 1
            print(f"FAIL {path.name}: telemetry opt-out missing")
    checks = len(SOURCES) * (len(WRITE_APIS) + 3)
    print(f"{checks - failures}/{checks} passed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
