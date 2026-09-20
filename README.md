# hffinish

Finish incomplete Hugging Face cache downloads and move complete models out of
the cache into a plain folder.

For every model in the hub cache (`~/.cache/huggingface/hub` by default) it:

1. checks the cached snapshot against the repo's file tree at that commit:
   every file present with the right size (`--checksum` also hashes each file
   against the Hub),
2. re-runs the download for anything missing, through `hfq` when that is on
   PATH (a queue wrapper that runs one `hf download` at a time), otherwise
   `hf download`; the download skips files that are already complete,
3. once complete, moves the files to `~/models/<name>` as plain files (renames
   on the same filesystem, no copies) and removes the cache entry.

A repo that another process is downloading right now is left alone. Pass
`--wait` to wait for it and move it when the download ends.

## Requirements

- [`uv`](https://docs.astral.sh/uv/): the script declares its own dependency
  (`huggingface_hub>=1.32`) and `uv run` installs it on first use. Without uv,
  run it with any Python 3.11+ that has that package.
- The `hf` CLI for downloads. Without it the script downloads in-process.

## Install

```sh
git clone <this repo> ~/Documents/GitHub/hffinish
ln -s ~/Documents/GitHub/hffinish/hffinish ~/.local/bin/hffinish
```

## Usage

```
hffinish                      full pass: check, resume, move
hffinish --dry-run            report only, change nothing
hffinish --wait               also wait for downloads that are in flight
hffinish org/name [...]       only repos whose id contains one of these strings
hffinish --no-move            check and resume, leave everything in the cache
hffinish --no-download        only move what is already complete
hffinish --merge              move into a destination folder that already exists
hffinish --checksum           hash every file against the Hub first (slow)
hffinish --layout nested      ~/models/<org>/<name> instead of ~/models/<name>
hffinish --dest DIR           another destination
hffinish --cache-dir DIR      another cache
hffinish --no-queue           call hf download directly instead of hfq
hffinish --poll SECONDS       interval for --wait (default 30)
```

Exit code 0 when every model is moved (or, with `--dry-run`, ready), 1 when
something is incomplete, skipped, or failed, 130 when interrupted.

## How it decides

- **Complete**: the file list and sizes come from `trees/<commit>.json`, which
  huggingface_hub writes next to each cached repo; when the file is absent the
  tree is fetched from the Hub. A pointer that is missing or dangling counts as
  missing; a file with the wrong size counts as corrupt and is deleted before
  the re-download so the downloader does not trust it.
- **In progress**: a process has the repo id on its command line, one of the
  repo's `.locks/*.lock` files is held, or a `*.incomplete` file was written in
  the last two minutes.
- **Resume**: huggingface_hub 1.32 never resumes a half-written file. Each
  attempt writes a fresh `<etag>.<uuid>.incomplete` and abandons the old one,
  so resuming means skipping finished files and restarting the rest. Stale
  partial files are deleted first; they are dead weight.
- **Move**: files are renamed into a hidden staging folder
  (`.hffinish-partial.<name>`), verified against the tree, and the folder is
  renamed into place at the end. Blobs that another cached snapshot also uses
  (huggingface_hub's shared blob store) are copied instead of renamed. An
  interrupted move is picked up on the next run. Datasets and spaces are
  listed but never moved.

## Test

`tests/e2e.sh` downloads `hf-internal-testing/tiny-random-gpt2` (about 12 MB)
into a scratch cache under `$TMPDIR`, damages it (missing blob, corrupt file,
dangling pointer, stale partial), and drives hffinish through detection,
resume, move, `--wait`, `--merge` with shared blobs, and the nested layout.
