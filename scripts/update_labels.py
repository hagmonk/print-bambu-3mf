#!/usr/bin/env python3
"""Generate a JSON mapping of Bambu Studio setting keys to user-facing labels."""

from __future__ import annotations

import argparse
import json
import re
from collections import Counter, defaultdict
from pathlib import Path
from typing import Dict, Iterable, List, Optional


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--studio-path",
        type=Path,
        default=Path("../nameplates/BambuStudio"),
        help="Path to the Bambu Studio source tree (default: ../nameplates/BambuStudio)",
    )
    parser.add_argument(
        "--po-file",
        type=Path,
        default=Path("BambuStudio_en.po"),
        help="Localization catalog to use for friendly text (default: BambuStudio_en.po)",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("src/bambu_3mf/data/labels.json"),
        help="Destination JSON file for the extracted labels",
    )
    return parser.parse_args()


def load_po_strings(po_path: Path) -> Dict[str, str]:
    """Build a msgid -> msgstr mapping from a PO file."""

    if not po_path.exists():
        return {}

    catalog: Dict[str, str] = {}

    with po_path.open("r", encoding="utf-8") as handle:
        current_id: List[str] = []
        current_str: List[str] = []
        mode: Optional[str] = None

        def flush() -> None:
            if not current_id:
                return
            msgid = "".join(current_id)
            msgstr = "".join(current_str)
            catalog[msgid] = msgstr

        for raw_line in handle:
            line = raw_line.rstrip("\n")

            if line.startswith("#"):
                continue

            if line.startswith("msgid "):
                flush()
                mode = "id"
                current_id = [extract_po_string(line[6:].strip())]
                current_str = []
                continue

            if line.startswith("msgstr "):
                mode = "str"
                current_str = [extract_po_string(line[7:].strip())]
                continue

            if line.startswith('"') and mode:
                if mode == "id":
                    current_id.append(extract_po_string(line))
                else:
                    current_str.append(extract_po_string(line))
                continue

            if not line:
                flush()
                mode = None
                current_id = []
                current_str = []

        flush()

    # Prefer msgstr when available, otherwise fall back to msgid.
    return {msgid: (msgstr or msgid) for msgid, msgstr in catalog.items()}


def extract_po_string(token: str) -> str:
    token = token.strip()
    if token.startswith('"') and token.endswith('"'):
        token = token[1:-1]
    return bytes(token, "utf-8").decode("unicode_escape")


def decode_cpp_string(expr: str) -> Optional[str]:
    """Decode a C++ string literal expression into Python text."""

    expr = expr.split("//", 1)[0]
    expr = expr.replace("L(", "")
    tokens = re.findall(r'"([^"\\]*(?:\\.[^"\\]*)*)"', expr)
    if not tokens:
        return None
    decoded = "".join(bytes(token, "utf-8").decode("unicode_escape") for token in tokens)
    decoded = decoded.encode("latin1", errors="ignore").decode("utf-8", errors="ignore")
    return decoded.strip()


def harvest_definitions(source: Path) -> Dict[str, Dict[str, str]]:
    """Extract key -> metadata mappings from a config definition file."""

    mapping: Dict[str, Dict[str, str]] = {}

    if not source.exists():
        return mapping

    with source.open("r", encoding="utf-8", errors="ignore") as handle:
        lines = handle.readlines()

    add_pattern = re.compile(r"\bthis->add(?:_nullable)?\s*\(\s*\"([^\"]+)\"")

    index = 0
    current_key: Optional[str] = None

    while index < len(lines):
        line = lines[index]
        match = add_pattern.search(line)
        if match:
            current_key = match.group(1)
            mapping.setdefault(current_key, {})
            index += 1
            continue

        if current_key:
            for attribute in ("label", "full_label", "tooltip", "category", "sidetext"):
                attr_marker = f"def->{attribute}"
                if attr_marker in line and "=" in line:
                    expr, offset = collect_expression(lines, index, attr_marker)
                    if expr is not None:
                        value = decode_cpp_string(expr)
                        if value:
                            mapping[current_key].setdefault(attribute, value)
                    index = offset
                    break
            else:
                handled = False
                for call in ("enum_values", "enum_labels"):
                    if f"def->{call}" in line and ("push_back" in line or "emplace_back" in line):
                        argument = extract_call_argument(line)
                        if argument is not None:
                            mapping[current_key].setdefault(call, []).append(argument)
                        index += 1
                        handled = True
                        break
                if not handled:
                    index += 1
        else:
            index += 1

    return mapping


def collect_expression(lines: List[str], start: int, marker: str) -> tuple[Optional[str], int]:
    """Collect the right-hand side expression following an assignment until ';'."""

    line = lines[start]
    rhs = line.split("=", 1)[1].strip()
    collected = [rhs]
    index = start

    while ";" not in collected[-1] and index + 1 < len(lines):
        index += 1
        collected.append(lines[index].strip())

    expression = " ".join(collected)
    if ";" in expression:
        expression = expression.split(";", 1)[0]
        return expression.strip(), index + 1
    return None, index + 1


def extract_call_argument(line: str) -> Optional[str]:
    match = re.search(r"\(([^()]+)\)\s*;", line)
    if not match:
        return None
    return decode_cpp_string(match.group(1))


def harvest_ui_groups(studio_path: Path) -> Dict[str, Dict[str, str]]:
    mapping: Dict[str, Dict[str, str]] = {}
    tab_path = studio_path / "src" / "slic3r" / "GUI" / "Tab.cpp"
    if not tab_path.exists():
        return mapping

    current_category: Optional[str] = None
    current_group: Optional[str] = None
    current_section: Optional[str] = None

    brace_depth = 0
    scope_stack: List[tuple[int, str]] = []
    pending_scope: Optional[str] = None
    scope_map = {
        "TabPrint": "process",
        "TabPrintModel": "process",
        "TabPrintObject": "process",
        "TabPrintPart": "process",
        "TabPrintLayer": "process",
        "TabFilament": "filament",
        "TabPrinter": "machine",
    }
    func_pattern = re.compile(r"\b(Tab\w+)::(~?\w+)\s*\(")

    with tab_path.open("r", encoding="utf-8", errors="ignore") as handle:
        for raw_line in handle:
            line = raw_line.strip()

            func_match = func_pattern.search(raw_line)
            if func_match:
                cls_name, method_name = func_match.groups()
                if not method_name.startswith("~"):
                    pending_scope = scope_map.get(cls_name)

            page_match = re.search(r'add_options_page\s*\(\s*L\("([^\"]+)"\)', line)
            if page_match:
                current_category = page_match.group(1)
                current_group = None
                current_section = None
                continue

            group_match = re.search(r'new_optgroup\s*\(\s*L\("([^\"]+)"\)', line)
            if group_match:
                current_group = group_match.group(1)
                current_section = None
                continue

            if line.startswith("Line line"):
                section_match = re.search(r'L\("([^\"]+)"\)', line)
                current_section = section_match.group(1) if section_match else None
                continue

            if 'optgroup->append_line(line' in line:
                current_section = None
                continue

            if 'line.append_option' in line:
                key_match = re.search(r'"([^\"]+)"', line)
                if key_match:
                    entry = mapping.setdefault(key_match.group(1), {})
                    if current_category:
                        entry['category'] = current_category
                    if current_group:
                        entry['group'] = current_group
                    if current_section:
                        entry['section'] = current_section
                    if scope_stack:
                        scope = scope_stack[-1][1]
                    else:
                        scope = pending_scope
                    if scope:
                        existing_scope = entry.get('scope')
                        if existing_scope and existing_scope != scope:
                            if isinstance(existing_scope, list):
                                if scope not in existing_scope:
                                    existing_scope.append(scope)
                            else:
                                entry['scope'] = sorted({existing_scope, scope})
                        else:
                            entry.setdefault('scope', scope)
                continue

            if 'optgroup->append_single_option_line' in line:
                key_match = re.search(r'"([^\"]+)"', line)
                if key_match:
                    entry = mapping.setdefault(key_match.group(1), {})
                    if current_category:
                        entry['category'] = current_category
                    if current_group:
                        entry['group'] = current_group
                    if scope_stack:
                        scope = scope_stack[-1][1]
                    else:
                        scope = pending_scope
                    if scope:
                        existing_scope = entry.get('scope')
                        if existing_scope and existing_scope != scope:
                            if isinstance(existing_scope, list):
                                if scope not in existing_scope:
                                    existing_scope.append(scope)
                            else:
                                entry['scope'] = sorted({existing_scope, scope})
                        else:
                            entry.setdefault('scope', scope)
                continue

            open_braces = raw_line.count('{')
            close_braces = raw_line.count('}')

            if pending_scope and open_braces > 0:
                scope_stack.append((brace_depth, pending_scope))
                pending_scope = None

            brace_depth += open_braces - close_braces

            while scope_stack and brace_depth <= scope_stack[-1][0]:
                scope_stack.pop()

    return mapping


def default_friendly_name(key: str) -> str:
    if not key:
        return key
    parts = re.split(r'[_/]+', key)
    words = []
    for part in parts:
        if not part:
            continue
        if part.isupper() or part.isdigit():
            words.append(part)
        else:
            words.append(part.capitalize())
    return " ".join(words) if words else key


def merge_mappings(mappings: Iterable[Dict[str, Dict[str, str]]]) -> Dict[str, Dict[str, str]]:
    result: Dict[str, Dict[str, str]] = {}
    for mapping in mappings:
        for key, data in mapping.items():
            target = result.setdefault(key, {})
            for attr, value in data.items():
                target.setdefault(attr, value)
    return result


def apply_localization(raw: Dict[str, Dict[str, str]], strings: Dict[str, str]) -> Dict[str, Dict[str, str]]:
    for data in raw.values():
        label_candidates = [data.get("full_label"), data.get("label")]
        friendly = None
        for candidate in label_candidates:
            if candidate:
                friendly = strings.get(candidate, candidate)
                if friendly:
                    break
        if not friendly:
            friendly = next((c for c in label_candidates if c), None)
        if friendly:
            data["friendly"] = friendly.strip()

        if "tooltip" in data and data["tooltip"]:
            data["tooltip"] = strings.get(data["tooltip"], data["tooltip"]).strip()
        if "sidetext" in data and data["sidetext"]:
            data["sidetext"] = strings.get(data["sidetext"], data["sidetext"]).strip()

        for field in ("category", "group", "section"):
            if field in data and data[field]:
                data[field] = strings.get(data[field], data[field]).strip()

        if "enum_labels" in data:
            localized_labels = [strings.get(item, item).strip() for item in data["enum_labels"]]
            data["enum_labels"] = localized_labels
            if "enum_values" in data and len(data["enum_values"]) == len(localized_labels):
                enum_map = {}
                for raw_value, label in zip(data["enum_values"], localized_labels):
                    enum_map[str(raw_value)] = label
                data["enum_map"] = enum_map
            data.pop("enum_values", None)
            data.pop("enum_labels", None)
    return raw


def ensure_unique_friendly_names(mapping: Dict[str, Dict[str, str]]) -> None:
    for key, data in mapping.items():
        if not data.get("friendly"):
            data["friendly"] = default_friendly_name(key)

    counts = Counter(data.get("friendly") for data in mapping.values() if data.get("friendly"))
    for key, data in mapping.items():
        friendly = data.get("friendly")
        if friendly and counts[friendly] > 1:
            data["friendly"] = f"{friendly} ({key})"


def main() -> None:
    args = parse_arguments()

    sources = sorted((args.studio_path / "src" / "libslic3r").rglob("*.cpp"))
    partial = [harvest_definitions(path) for path in sources]
    merged = merge_mappings(partial)

    ui_groups = harvest_ui_groups(args.studio_path)
    for key, info in ui_groups.items():
        target = merged.setdefault(key, {})
        for field, value in info.items():
            target.setdefault(field, value)

    po_strings = load_po_strings(args.po_file)
    localized = apply_localization(merged, po_strings)
    ensure_unique_friendly_names(localized)

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(localized, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
