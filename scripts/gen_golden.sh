#!/usr/bin/env bash
# Generate golden reference outputs from the *Python* implementation.
# These lock the Go port to byte-identical behavior.
#
# NOTE: output depends on the local BambuStudio profile directory
# (~/Library/Application Support/BambuStudio). Regenerate on the same
# machine/profile set used for the Go golden tests.
set -euo pipefail
cd "$(dirname "$0")/.."

OUT=testdata/golden
mkdir -p "$OUT"

# Runner: $1=label, rest=args. Captures stdout; records exit code + stderr.
run() {
  local label="$1"; shift
  local file="$OUT/$label"
  set +e
  uv run bambu-3mf "$@" >"$file.out" 2>"$file.err"
  local rc=$?
  set -e
  echo "$rc" >"$file.rc"
  # Drop empty stderr/rc noise files to keep the tree tidy (rc 0 + empty err is the common case).
  if [[ ! -s "$file.err" ]]; then rm -f "$file.err"; fi
  if [[ "$rc" == "0" ]]; then rm -f "$file.rc"; fi
  printf '  %-48s rc=%s\n' "$label" "$rc"
}

for path in samples/*.3mf; do
  base=$(basename "$path" .3mf)
  echo "$base:"
  run "${base}__default"            "$path"
  run "${base}__friendly"           "$path" --friendly-names
  run "${base}__full"               "$path" --full
  run "${base}__full-friendly"      "$path" --full --friendly-names
  run "${base}__no-inheritance"     "$path" --no-inheritance
  run "${base}__select-mp"          "$path" --select machine,process
  run "${base}__select-f"           "$path" --select filament
  run "${base}__list"               "$path" --list
  run "${base}__obj0"               "$path" --object 0
  run "${base}__obj0-friendly"      "$path" --object 0 --friendly-names
  run "${base}__obj0-full"          "$path" --object 0 --full
  run "${base}__obj0-select-p"      "$path" --object 0 --select process
done

# Per-plate sweeps for the multi-plate fixtures.
for path in samples/per-object.3mf samples/saved-process.3mf samples/storz.3mf; do
  base=$(basename "$path" .3mf)
  echo "$base (plates):"
  for n in 1 2 3 4 5; do
    run "${base}__plate${n}"          "$path" --plate "$n"
    run "${base}__plate${n}-friendly" "$path" --plate "$n" --friendly-names
  done
done

echo "Done. $(find "$OUT" -name '*.out' | wc -l | tr -d ' ') golden files."
