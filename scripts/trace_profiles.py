#!/usr/bin/env python3
"""Trace every profile JSON the extractor reads across the sample matrix, and
copy them into testdata/profiles/ so the Go golden tests can run hermetically
via BAMBU_PROFILE_DIR.

Captures the real on-disk layout the extractor expects:
  <root>/user/<id>/<type>/<name>.json        (+ base/ subdir)
  <root>/system/BBL/<type>/<name>.json       (+ base/ subdir)
"""
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from bambu_3mf.extractor import BambuProfileExtractor  # noqa: E402

REPO = Path(__file__).resolve().parents[1]
DEST = REPO / "testdata" / "profiles"

opened: set[Path] = set()
_orig_open = open  # builtin


def _patched_load(self, profile_name: str, profile_type: str = "filament"):
    # Re-implement the path probing of load_profile, but record hits.
    cache_key = f"{profile_type}:{profile_name}"
    if cache_key in self.profiles_cache:
        return self.profiles_cache[cache_key]
    name = profile_name.replace(".json", "")
    candidates = []
    user_id = self.find_user_id()
    if user_id:
        d = self.user_dir / user_id / profile_type
        candidates.append(d / f"{name}.json")
        candidates.append(d / "base" / f"{name}.json")
    sd = self.system_dir / profile_type
    candidates.append(sd / f"{name}.json")
    candidates.append(sd / "base" / f"{name}.json")
    import json as _json
    for c in candidates:
        if c.exists():
            opened.add(c)
            with _orig_open(c, "r") as f:
                profile = _json.load(f)
                self.profiles_cache[cache_key] = profile
                return profile
    return None


BambuProfileExtractor.load_profile = _patched_load

samples = sorted((REPO / "samples").glob("*.3mf"))
for path in samples:
    ext = BambuProfileExtractor()
    for plate in (None, 1, 2, 3, 4, 5):
        ext.extract_structured_from_3mf(path, plate_number=plate, include_gcode=True)
        ext.extract_structured_from_3mf(path, plate_number=plate, friendly_names=True)
    for sel in ("0", "1", "2", "3"):
        ext.get_object_settings(path, sel, include_gcode=True)

home = Path.home()
sys_root = home / "Library" / "Application Support" / "BambuStudio" / "system"
user_root = home / "Library" / "Application Support" / "BambuStudio" / "user"

count = 0
for src in sorted(opened):
    if str(src).startswith(str(sys_root)):
        rel = Path("system") / src.relative_to(sys_root)
    elif str(src).startswith(str(user_root)):
        rel = Path("user") / src.relative_to(user_root)
    else:
        print(f"  SKIP (outside known roots): {src}", file=sys.stderr)
        continue
    dst = DEST / rel
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)
    count += 1

print(f"Copied {count} profile files into {DEST.relative_to(REPO)}")
