# HF-Downloader / hffinish

`hffinish` finishes incomplete Hugging Face cache downloads and moves complete
models out of the cache into a plain folder. **HF-Downloader** is its Qt 6
desktop window (PySide6): download a model, finish what is in the cache, watch
the log, all without a terminal. See [Windows](#windows).

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

## Download

| Installer | Version | Size | SHA-256 |
| --- | --- | --- | --- |
| [`HF-Downloader-setup-1.2.0.exe`](https://github.com/adams7100/hf-gui/releases) | 1.2.0 | 56 MB | `8c0b96f2e2a908b4b265f851332958250a03a563defc872762c20d6581c7d283` |

Windows 10/11, 64-bit, no Python needed. Installers are attached to the
[Releases](https://github.com/adams7100/hf-gui/releases) page; each one is
built from the tagged commit with `build-installer.cmd` (see
[Installer](#installer)) and installs per user without an admin prompt. The
file is unsigned, so SmartScreen may warn once; choose "More info" and "Run
anyway".

## Requirements

- [`uv`](https://docs.astral.sh/uv/): the script declares its own dependency
  (`huggingface_hub>=1.32`) and `uv run` installs it on first use. Without uv,
  run it with any Python 3.11+ that has that package.
- The `hf` CLI for downloads. Without it the script downloads in-process.

On Windows none of this needs setting up by hand: see [Windows](#windows).

## Install

macOS and Linux:

```sh
git clone <this repo> ~/Documents/GitHub/hffinish
ln -s ~/Documents/GitHub/hffinish/hffinish ~/.local/bin/hffinish
```

Windows: install `HF-Downloader-setup-<version>.exe` (built by
`build-installer.cmd`), or clone the repo and start `HF-Downloader.cmd` (the
window) or `hffinish.cmd` (the command line) from it. Details under
[Windows](#windows).

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

## Windows

The script runs on Windows with Python 3.11 or newer. The platform differences
are handled inside it:

- the hub cache has no symlinks unless Developer Mode is on, so snapshot files
  are plain files and the move renames them directly,
- "in progress" detection reads process command lines through
  `Get-CimInstance Win32_Process` instead of `/proc`, and tests download locks
  with `msvcrt.locking`, which is what huggingface_hub's file lock uses there,
  instead of `flock`,
- the default destination is `%USERPROFILE%\models`, the default cache
  `%USERPROFILE%\.cache\huggingface\hub` (or `HF_HUB_CACHE` / `HF_HOME`
  when set, as everywhere else).

### HF-Downloader (the window)

`hf_downloader.py` is a Qt 6 (PySide6) front end for the same script, with
three tabs.

**Download** tab:

- **Download a model**: paste the model in whatever form you have it and click
  Download. Accepted: `org/name`, `hf download org/name [--revision r]`,
  `huggingface-cli download org/name`, `https://huggingface.co/org/name`,
  the same with `?clone=true`, `/tree/<rev>`, `/blob/<rev>/file` or
  `/resolve/<rev>/file`, `git clone https://huggingface.co/org/name[.git]`,
  `hf.co/org/name`. The field is normalised to `org/name` and a revision in
  the link fills the Revision box. The download runs `hf download` into the
  hub cache with live output; with "then move it to the library" checked, the
  finish pass follows and the model ends up in `~/models/<name>` as plain
  files.
- **Download a list**: paste any number of repos, one per line in any of the
  forms above (blank lines and `#` comments are skipped, duplicates dropped),
  and click "Download all". Lines that are not repos are shown before the run
  starts and skipped. The list is kept between starts, and the Hub browser's
  "Add to list" appends to it.
- **Unfinished downloads survive a restart**: every running or queued download
  is remembered (in the app's settings), as is any that stopped or failed.
  When the window opens again a banner on the Download tab lists them with a
  "Resume all" button, which picks each one up where it stopped (what is
  already in the cache is kept, only the missing files are fetched, then the
  model is verified and moved), or "Forget them". Resuming or finishing a
  download removes it from the list.
- **Library folder by typing**: the library folder field on the Download tab
  and the one at the top of the Libraries tab are the same setting; type a
  path into either (or Browse) and it applies as soon as you leave the field:
  the Libraries list rescans and the Browse tab's Downloaded column is
  re-checked. A folder that does not exist yet is created when the first
  model is moved into it.
- **Several at once**: downloads run side by side, as many as the "download N
  at once" box allows (default 3); the rest wait in a queue and start as slots
  free up. Each running download has its own row in the progress panel with
  its own Stop button, and the table marks queued repos as "queued". Each one
  is verified and moved on its own when "then move it to the library" is on.
  More downloads can be added while others run; only "Check (dry run)" and
  "Finish and move", which walk the whole cache, wait until nothing else runs.
- **Progress**: while a download runs, a progress bar shows bytes in the cache
  against the size the Hub reports for the repo, with the download rate into
  the cache as bytes and bits per second (`85.3 MB/s (682 Mbit/s)`), an ETA,
  and the machine's network throughput in both directions (download and
  upload, from `psutil`). The meter watches the repo's blobs folder once a
  second, so it is exact whatever downloader is at work, including a resume
  started by the finish pass.
- **Heartbeat**: the meter is also a watchdog. Every second it checks that
  the byte count on disk is still moving and that the downloading process is
  still alive, and says so next to the rate: "receiving data" (green), "no
  data for N s" (amber after 20 s), "stalled" (red after 90 s, also logged)
  or "process gone". The rate itself is shown large, as bytes and bits per
  second, and drops to 0 B/s as soon as nothing has landed for 3 s instead of
  fading slowly.
- **Downloads never stay stalled**: the heartbeat is also a watchdog for the
  app's own downloads. When no byte has landed for 90 s it ends the download
  command and starts it again (complete files are skipped by the downloader,
  stale partial files are dropped first), as often as needed; a download that
  exits with an error is retried up to 5 times. The wait between attempts
  grows from 5 s to 60 s, the table shows "restarting", and every restart is
  logged. Stop still ends everything at once.
- **Three checksums per download**: before the download the Hub's checksum
  of every file is fetched and logged (sha256 for LFS files, git blob sha1 for
  small files). After the download every file is hashed in the cache and
  compared; files that do not match are dropped and fetched again (up to 3
  rounds). After the move every file is hashed once more in the library. All
  digests go to the log, and a pop-up at the end shows the three stages with
  a tick or a cross and a per-file table of the hashes ("Copy report" puts
  it on the clipboard). The hffinish checksum option is skipped for these
  runs because the files were hashed moments before.
- **Another process's download**: when the finish pass finds a repo that
  another program is downloading (your own `hf download` in a terminal, for
  example), the window attaches the same meter to that download and shows
  its progress, rate and heartbeat in the panel, naming the process id. When
  that process ends the repo is verified automatically. A "Stop other
  process" button ends it if you would rather continue the download here with
  Resume.
- **Resume**: a stopped download (Stop, crash, lost network, closed window)
  is picked up with the Resume button next to Download, or "Resume download"
  on the Libraries tab. Complete files are kept; partial `*.incomplete` files
  are removed first because huggingface_hub never appends to one (every
  download goes to a fresh uniquely named temporary file), and only the
  missing files are fetched again, at the commit the cache already holds.
  Then the model is verified and moved. A repo that is not in the cache yet
  is simply downloaded.
- **Verification when a download stops**: if the download fails, is stopped,
  or is not followed by the finish pass, the window immediately checks that
  repo in the cache file by file against the Hub's file list and reports
  `incomplete, N file(s) to fetch` or `complete`. Clicking Download again
  resumes (files that are already complete are skipped). The same check runs
  for every cached model when the window opens ("startup check"), so a
  download interrupted by a crash or a closed window is flagged before
  anything else happens.
- **Finish and move cached models**: the `hffinish` options as fields and
  check boxes (cache, library folder, filter, layout, merge, checksum, wait,
  ...). "Verify checksums" is on by default, so every file is hashed against
  the Hub before a model is moved out of the cache; untick it for a faster,
  size-only check. "Check (dry run)" verifies and reports, "Finish and move"
  resumes and moves. Every repo appears in the table with its status and
  size; the log below shows what the script prints. Stop ends the run after
  the current step (it kills a running `hf download`). Every log line is
  also written to `hf-downloader.log` in the app's local data folder
  (`%LOCALAPPDATA%\HF-Downloader\HF-Downloader` on Windows), across sessions,
  rotated at 20 MB; "Open log" (or File > Open log file, Ctrl+L) opens it in
  your editor, "Clear log" only clears the view.

**Browse Hub** tab: the Hub's model listing, fetched through the same API the
website uses. Search words, author, task (pipeline tag) and tags filter it;
"Sort by" picks the order the Hub returns (trending, downloads, likes,
updated, created). "Scrape page 1" fetches the first page, "Next page"
appends the following one, as often as you like. Every column (model, author,
task, library, downloads last month, all-time downloads, likes, trending,
updated, created, gated, tags) sorts by clicking its header, numbers and
dates numerically. Several rows can be selected at once (Ctrl-click, Shift-click, Ctrl+A for the
page): "Download" queues every selected model on the Download tab (asking
first about the ones already downloaded), "Add to list" appends them all to
the list there, "Open on huggingface.co" opens a tab for each. "View details"
(or a double click) opens the model card window for the first selected one;
while that window is open it follows the selection, switching to whichever
model you select next (in the Hub browser, or a cached model on the
Libraries tab) without taking the focus away from the table: the whole README rendered with its
images, the front-matter facts (license, base model, language, tags, ...),
download, like and task statistics, and the file list with sizes. Nothing of
this touches the hub cache; the card and its pictures are fetched directly.

The legend chips above the table are buttons: click a category (Text,
Image, ...) to show only that category's models, sorted by task so models
of one kind sit together; click a download state to show only models in
that state; click a chip again to show everything. In the model card
window the card is converted to HTML with the `markdown` package (tables,
fenced code, inline HTML; collapsed `<details>` blocks are opened) and shown
with a stylesheet that keeps links readable in dark and light themes. The
file list has a "View file" button (also a double-click) for text files:
`.json` is pretty-printed, `.md` is rendered (with a "Show source" toggle),
`.txt`, `.yaml`, `.py` and the like are shown as text; files above 8 MB and
binary files are left to "Open on huggingface.co".

The **Downloaded** column says whether a listed model is already on this
machine: "in library" (a folder with files in the library, in either
layout), "in cache" (complete in the hub cache), "incomplete",
"downloading" (another process is fetching it) or "unverified". It is
checked on disk for every page and again after every job. Asking to download
a model that is "in library" or "in cache", from any button or from a pasted
list, brings up a pop-up that says so and asks whether you are sure; for a
list you can also skip the ones already downloaded. The Task column is
coloured by **category** (Multimodal, Text, Image, Video, Audio, Embeddings,
Agents, Other, with a legend above the table), and the model card window
shows the same colour. Every coloured cell is a solid box whose text is the
box colour's opposite: the complementary hue, pale on a dark box and deep on
a light one, so it reads clearly in both themes. The window itself carries
the Hugging Face orange as accent on tabs, primary buttons, progress bars
and table headers, on top of the system's light or dark theme.

**Libraries** tab: every model folder in the library (the destination), with
state, an action button, file count, size, last change, details and path. The
State column says whether a model is finished: "finished" (in the library),
"finished, in cache" (every file there with the right size, not moved yet),
"not finished" (how many files and bytes are still to fetch, plus any partial
files a stopped download left), "downloading" (another process is fetching it
right now: a held download lock or a partial file written in the last two
minutes), "moving" (a folder an interrupted move left behind) or "unverified"
(no file list on disk yet). Cached models are checked file by file against
the file list huggingface_hub keeps next to the snapshot, without touching
the network. Every row carries its own button: "Resume download" for a model
that is not finished, "Move to library" for one finished in the cache, "Open
folder" for one in the library. "Verify" above the table runs the full check
against the Hub for the selected cached model. Double-click a row or use
"Open model folder" to open it in Explorer. The list refreshes when the tab
is shown and after every job.

Settings are remembered between starts. Start it with `HF-Downloader.cmd`
(creates `.venv` with `huggingface_hub`, `PySide6`, `psutil` and `markdown` on first run)
or `python hf_downloader.py`. `tests\test_parse.py` checks the link and list
parsers and the model card helpers.

### Run it (command line)

`hffinish.cmd` is the launcher. Every argument goes straight through to the
script, so the usage above applies unchanged:

```bat
hffinish.cmd --dry-run
hffinish.cmd org/name --wait
```

What the launcher does the first time:

1. If `uv` is on PATH it runs the script through `uv run --script`, which
   installs `huggingface_hub` by itself, and nothing else is created.
2. Otherwise it looks for Python 3.11+ (`py -3`, then `python`; the Microsoft
   Store Python works), creates a private virtual environment in `.venv` next
   to the script, and installs `huggingface_hub` into it. This takes about half
   a minute once; later runs start immediately.

Downloads use the `hf.exe` already on PATH when there is one (so `hf_xet`
and your login are picked up), otherwise huggingface_hub downloads in-process.

To call it from anywhere, add the repo folder to PATH or create a shortcut to
`hffinish.cmd`.

### Standalone executables

`build-exe.cmd` packages everything with PyInstaller, no Python needed on the
machine it runs on:

- `dist\hffinish.exe`: the command line, one file (about 18 MB),
- `dist\HF-Downloader\HF-Downloader.exe` plus `_internal\`: the window, one
  folder (about 100 MB because of Qt; a folder build so it starts at once).

```bat
HF-Downloader.cmd        :: once, so .venv exists (hffinish.cmd works too)
build-exe.cmd            :: installs PyInstaller/PySide6 into .venv if needed, builds, smoke-tests
dist\hffinish.exe --dry-run
dist\HF-Downloader\HF-Downloader.exe
```

The icon in `assets\` is drawn by `make_icon.py` (run it to change it).
Intermediate build files go to `%TEMP%\hffinish-build`; `.venv`, `dist` and
`build` are ignored by git. Rebuild after changing the script. Windows
SmartScreen may warn the first time an unsigned exe runs; that is expected for
a locally built binary.

### Installer

`build-installer.cmd` wraps the exe in a Windows installer built with
[Inno Setup](https://jrsoftware.org/isinfo.php) (`winget install
JRSoftware.InnoSetup`). It runs `build-exe.cmd` first, then compiles
`installer.iss`:

```bat
build-installer.cmd            :: dist\HF-Downloader-setup-1.2.0.exe
build-installer.cmd 1.3.0      :: another version number
```

The installer puts `HF-Downloader.exe` (with its `_internal\` folder),
`hffinish.exe`, the README and the license under
`%LOCALAPPDATA%\Programs\HF-Downloader` (per user, no admin prompt; choose
"all users" on the first page for `C:\Program Files`). It adds
"HF-Downloader" to the Start Menu (and to the desktop when that task is
checked), adds the folder to PATH when the "Add to PATH" task is left checked
so `hffinish` works from any terminal, offers to launch the window at the end,
and registers "HF-Downloader" in Settings > Apps with an uninstaller that
removes the PATH entry again. An install made by an earlier build under the
name "hffinish" is removed automatically.

Open a **new** terminal after installing: terminals that were already open
keep their old PATH and will not find `hffinish`. Silent install:

```bat
HF-Downloader-setup-1.2.0.exe /VERYSILENT /NORESTART
```

The installer is unsigned, like the exe, so SmartScreen may warn once
("Windows protected your PC"); choose "More info" and "Run anyway".

### Not on Windows

`hfq` (the download queue) is a POSIX tool; the launcher runs `hf download`
directly, which is the same as passing `--no-queue`. `tests/e2e.sh` needs bash
(Git Bash or WSL) and a POSIX `touch`, `readlink -f` and `exec -a`; run it
under WSL.

## Test

`tests/e2e.sh` downloads `hf-internal-testing/tiny-random-gpt2` (about 12 MB)
into a scratch cache under `$TMPDIR`, damages it (missing blob, corrupt file,
dangling pointer, stale partial), and drives hffinish through detection,
resume, move, `--wait`, `--merge` with shared blobs, and the nested layout.
