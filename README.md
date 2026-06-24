# print-bambu-3mf

A CLI that dumps the printer, process, and filament settings out of a Bambu
Studio `.3mf` file as structured JSON suitable for human or LLM inspection. It
resolves the full preset inheritance chain (against embedded presets first, then
your local BambuStudio profile directory), applies project- and per-object
overrides, and groups everything into `machine` / `process` / `filaments` /
`plates` blocks. Optional `--friendly-names` swaps raw config keys for the
labels and category names used in the BambuStudio UI.

A single static binary written in Go — no runtime dependencies.

## Install

```bash
brew install hagmonk/tap/print-bambu-3mf
```

Or build from source (Go 1.24+):

```bash
go install github.com/hagmonk/print-bambu-3mf/cmd/print-bambu-3mf@latest
```

## Usage

```bash
# Full structured dump
print-bambu-3mf path/to/file.3mf

# List plates and objects
print-bambu-3mf file.3mf --list

# One plate only
print-bambu-3mf file.3mf --plate 1

# Per-object flattened settings
print-bambu-3mf file.3mf --object 0
print-bambu-3mf file.3mf --object "part_name"

# Filter sections
print-bambu-3mf file.3mf --select machine,process

# Friendly UI labels and categories instead of raw keys
print-bambu-3mf file.3mf --friendly-names

# Include gcode-related settings (filtered out by default)
print-bambu-3mf file.3mf --full
```

The output schema is:

```
{
  "machine":   { "id": <preset>, "model_id": <"N6" | null>, "settings": {...} },
  "process":   { "id": <preset>, "settings": {...} },
  "filaments": { <slot>: { "id": <preset>, "settings": {...} }, ... },
  "plates":    { <n>:    { "objects": [ { name, id, index, extruder,
                                          process_id, filament_id,
                                          process_overrides }, ... ] } }
}
```

`machine.model_id` is the canonical hardware identifier (e.g. `N6` for X2D,
written by BambuStudio into `slice_info.config` on slice export). It is `null`
for plain project saves, since BambuStudio only writes it after a slice.

## Supported printers

The tool is machine-agnostic: any printer whose presets resolve via the local
BambuStudio profile directory works without code changes. Tested against X1
Carbon and H2D fixtures; X2D (N6), H2D Pro, A1, P1S, etc. all extract the same
way provided their profiles exist on disk.

For the inheritance chain to resolve, BambuStudio's system profiles must be
installed at:

```
~/Library/Application Support/BambuStudio/system/BBL/
```

(macOS path; the extractor reads from there.) Point the tool at a different
profile tree by setting `BAMBU_PROFILE_DIR` to a directory containing
`system/BBL/...` and `user/<id>/...`. 3MFs whose presets are fully embedded
resolve without any local profiles.

## Development

```bash
go build ./...      # compile
go test ./...       # golden tests: CLI output vs Python-captured fixtures
```

Golden tests live in `internal/bambu/golden_test.go`. They run the CLI over
`samples/*.3mf` across the full flag matrix and compare stdout byte-for-byte
against `testdata/golden/`, resolving presets against the hermetic profile
fixture in `testdata/profiles/` (via `BAMBU_PROFILE_DIR`) so they need no local
BambuStudio install. The fixtures were captured from the original Python
implementation; regenerate them with `scripts/gen_golden.sh` and refresh the
profile fixture with `scripts/trace_profiles.py`.

## Refreshing UI labels

`internal/bambu/labels.json` maps raw config keys to friendly names, categories,
units, and enum value labels, and is embedded into the binary at build time. It
is generated from the BambuStudio source tree, so it needs a refresh whenever
BambuStudio adds new printers or settings (e.g. on every release).

The **Update labels** GitHub Actions workflow automates this: trigger it with a
BambuStudio git ref (tag or branch). It sparse-shallow-clones only the paths the
generator needs, regenerates `labels.json`, and opens a PR. Spot-check the diff,
merge it, then push a `vX.Y.Z` tag to cut a release with the updated labels.

To regenerate locally instead:

```bash
uv run --no-project python scripts/update_labels.py \
  --studio-path /path/to/BambuStudio \
  --po-file     /path/to/BambuStudio/bbl/i18n/en/BambuStudio_en.po \
  --output      internal/bambu/labels.json
```

The script harvests setting metadata from `src/libslic3r/*.cpp` (mostly
`PrintConfig.cpp`) and UI grouping from `src/slic3r/GUI/Tab.cpp`, then localizes
labels against the English PO catalog. Entries without a UI binding get a
`friendly` label but no `scope`, and fall back to prefix-based scope inference
in the extractor.

## Releases

Tagging `vX.Y.Z` runs GoReleaser (`.goreleaser.yaml`), which builds a macOS
universal binary and publishes a Homebrew Cask to `hagmonk/homebrew-tap`.

The default `GITHUB_TOKEN` only has access to the repo running the workflow, so
pushing the Cask cross-repo needs a separate credential. The release workflow
mints a short-lived **GitHub App installation token** scoped to *only* the
`homebrew-tap` repo (via `actions/create-github-app-token`) — least-privilege,
not tied to any user, and auto-expiring. Setup:

1. Create a GitHub App (org or personal) with **Repository permissions →
   Contents: Read and write**. No webhook needed.
2. Install it on **only** the `homebrew-tap` repo.
3. Add two secrets to the `print-bambu-3mf` repo: `TAP_APP_ID` (the App's ID)
   and `TAP_APP_PRIVATE_KEY` (a generated private key, PEM contents).

A fine-grained PAT scoped to just `homebrew-tap` with `contents: write` is a
simpler fallback, but it is long-lived and user-bound; prefer the App token.

## Samples

`samples/` contains small fixtures used for smoke-testing across printer
families and nozzle sizes. Add new fixtures here when a printer family
introduces format or config-key changes you want regression coverage for.
