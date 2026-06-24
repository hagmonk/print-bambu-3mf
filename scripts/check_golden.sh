#!/usr/bin/env bash
# Diff Go CLI output against the Python-generated golden files.
# Mirrors the invocation matrix in gen_golden.sh.
set -uo pipefail
cd "$(dirname "$0")/.."

GOLD=testdata/golden
BIN=$(mktemp)
go build -o "$BIN" ./cmd/print-bambu-3mf || { echo "build failed"; exit 1; }
trap 'rm -f "$BIN"' EXIT

pass=0; fail=0; failed=()

check() {
  local label="$1"; shift
  "$BIN" "$@" >/tmp/go_got.out 2>/dev/null
  if diff -q "$GOLD/$label.out" /tmp/go_got.out >/dev/null 2>&1; then
    pass=$((pass+1))
  else
    fail=$((fail+1)); failed+=("$label")
  fi
}

for path in samples/*.3mf; do
  base=$(basename "$path" .3mf)
  check "${base}__default"        "$path"
  check "${base}__friendly"       "$path" --friendly-names
  check "${base}__full"           "$path" --full
  check "${base}__full-friendly"  "$path" --full --friendly-names
  check "${base}__no-inheritance" "$path" --no-inheritance
  check "${base}__select-mp"      "$path" --select machine,process
  check "${base}__select-f"       "$path" --select filament
  check "${base}__list"           "$path" --list
  check "${base}__obj0"           "$path" --object 0
  check "${base}__obj0-friendly"  "$path" --object 0 --friendly-names
  check "${base}__obj0-full"      "$path" --object 0 --full
  check "${base}__obj0-select-p"  "$path" --object 0 --select process
done
for path in samples/per-object.3mf samples/saved-process.3mf samples/storz.3mf; do
  base=$(basename "$path" .3mf)
  for n in 1 2 3 4 5; do
    check "${base}__plate${n}"          "$path" --plate "$n"
    check "${base}__plate${n}-friendly" "$path" --plate "$n" --friendly-names
  done
done

echo "PASS=$pass FAIL=$fail"
if [[ $fail -gt 0 ]]; then
  printf '  FAILED: %s\n' "${failed[@]}"
fi
