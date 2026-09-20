#!/usr/bin/env bash
set -uo pipefail
# End-to-end test: downloads hf-internal-testing/tiny-random-gpt2 (~12 MB) into a
# scratch cache, damages it, and drives hffinish through every code path.
# Needs network and the hf CLI. Scratch dir: $HFFINISH_TEST_DIR or $TMPDIR/hffinish-e2e.
export HF_HUB_DISABLE_PROGRESS_BARS=1
T=${HFFINISH_TEST_DIR:-${TMPDIR:-/tmp}/hffinish-e2e}
HFFINISH=${HFFINISH:-$(cd "$(dirname "$0")/.." && pwd)/hffinish}
rm -rf "$T"; mkdir -p "$T/cache" "$T/models"
R=hf-internal-testing/tiny-random-gpt2
RD=$T/cache/models--hf-internal-testing--tiny-random-gpt2
step(){ echo; echo "##### $*"; }
store_files(){ find "$T/cache/blobs" -type f 2>/dev/null | grep -v '\.lock$' | grep -v '\.refs$' | grep -vc 'huggingface-shared-blobs'; }

step "download tiny repo into scratch cache"
hf download $R --cache-dir "$T/cache" --format quiet || { echo "TEST ABORTED: download failed"; exit 1; }
SNAP=$(ls -d "$RD"/snapshots/*) || exit 1
ls -la "$SNAP" | sed 1d
echo "top-level cache: $(ls "$T/cache" | tr '\n' ' ')"; echo "shared store blobs: $(store_files)"

step "1. dry run on complete repo"
"$HFFINISH" --cache-dir "$T/cache" --dest "$T/models" --dry-run; echo "rc=$?"

step "1b. --wait: a fake process carrying the repo id ends after 6s (expect: waiting, then would move)"
( exec -a fakedl bash -c 'sleep 6; true' fakedl hf-internal-testing/tiny-random-gpt2 ) &
sleep 1; "$HFFINISH" --cache-dir "$T/cache" --dest "$T/models" --dry-run --wait --poll 2; echo "rc=$?"; wait

step "2. damage: drop largest file's pointer+blob, corrupt config.json, plant an old stale partial, make one pointer dangling"
BIG=$(cd "$SNAP" && ls -S | head -1); REAL=$(readlink -f "$SNAP/$BIG"); rm -f "$SNAP/$BIG" "$REAL"
CREAL=$(readlink -f "$SNAP/config.json"); printf '{}' > "$CREAL"
echo junk > "$RD/blobs/deadbeef.12345678.incomplete"; touch -d "10 minutes ago" "$RD/blobs/deadbeef.12345678.incomplete"
SMALL=$(cd "$SNAP" && ls -S | tail -1); rm -f "$(readlink -f "$SNAP/$SMALL")"
echo "removed $BIG, corrupted config.json, dangling $SMALL"

step "3. dry run on damaged repo (expect 2 missing, 1 bad, 1 stale partial)"
"$HFFINISH" --cache-dir "$T/cache" --dest "$T/models" --dry-run; echo "rc=$?"

step "4. real run: resume + move"
"$HFFINISH" --cache-dir "$T/cache" --dest "$T/models" --no-queue; echo "rc=$?"

step "5. dest contents (expect plain files, 0 symlinks, real config.json)"
find "$T/models" -type f -printf '%s %P\n' | sort -k2; echo "symlinks: $(find "$T/models" -type l | wc -l)"
echo "config.json head: $(head -c 60 "$T/models/tiny-random-gpt2/config.json")"

step "6. cache leftovers (expect no models-- dir, no locks dir, 0 store blobs)"
ls -A "$T/cache"; ls -A "$T/cache/.locks" 2>/dev/null; echo "shared store blobs left: $(store_files)"; find "$T/cache/blobs" -type f | grep -v huggingface-shared-blobs

step "7. second run: nothing cached"
"$HFFINISH" --cache-dir "$T/cache" --dest "$T/models"; echo "rc=$?"

step "8. shared-blob + existing-dest: re-download, add fake 2nd snapshot sharing the blobs"
hf download $R --cache-dir "$T/cache" --format quiet || { echo "TEST ABORTED: re-download failed"; exit 1; }
SNAP=$(ls -d "$RD"/snapshots/*) || exit 1; cp -a "$SNAP" "$RD/snapshots/0000000000000000000000000000000000000000"; cp "$RD/trees/$(basename "$SNAP").json" "$RD/trees/0000000000000000000000000000000000000000.json"
"$HFFINISH" --cache-dir "$T/cache" --dest "$T/models" --no-queue; echo "rc=$? (expect skipped: dest exists)"
"$HFFINISH" --cache-dir "$T/cache" --dest "$T/models" --no-queue --merge; echo "rc=$? (expect moved by copying, other revision stays)"
echo "snapshots left: $(ls "$RD/snapshots" | tr '\n' ' ')"; echo "dangling links in fake snapshot: $(find "$RD/snapshots/0000000000000000000000000000000000000000" -xtype l | wc -l)"
echo "files in dest: $(find "$T/models" -type f | wc -l)"

step "9. nested layout + filter + no-move, then a filter that matches nothing"
"$HFFINISH" --cache-dir "$T/cache" --dest "$T/models2" --layout nested --no-move tiny-random; echo "rc=$?"
"$HFFINISH" --cache-dir "$T/cache" --dest "$T/models2" nomatch; echo "rc=$?"

step "10. nested move of the remaining fake revision"
"$HFFINISH" --cache-dir "$T/cache" --dest "$T/models2" --layout nested --no-queue; echo "rc=$?"
find "$T/models2" -type f | head -3; echo "cache dirs left: $(ls -A "$T/cache" | tr '\n' ' ')"; echo "store files left:"; find "$T/cache/blobs" -type f | grep -v huggingface-shared-blobs; echo "(end)"
