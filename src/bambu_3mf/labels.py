"""Utilities for mapping raw preset keys to user-facing labels and groups."""

from __future__ import annotations

import json
import re
from functools import lru_cache
from importlib import resources
from typing import Any, Dict, Optional


class LabelMapper:
    """Lookup helper for transforming preset keys into friendly labels."""

    CATEGORY_ORDER = [
        "Quality",
        "Strength",
        "Speed",
        "Support",
        "Others",
        "Extruders",
        "Machine limits",
        "Cooling",
        "Advanced",
    ]

    def __init__(self) -> None:
        self._mapping = get_label_metadata()
        self._category_index = {name: idx for idx, name in enumerate(self.CATEGORY_ORDER)}

    def friendly_key(self, key: str) -> str:
        entry = self._mapping.get(key)
        if entry:
            for candidate in ("friendly", "full_label", "label"):
                value = entry.get(candidate)
                if value:
                    return value
        return self._fallback_label(key)

    def transform(self, settings: Dict[str, Any]) -> Dict[str, Any]:
        structured: Dict[Optional[str], Dict[str, Any]] = {}
        has_category = False

        for key, value in settings.items():
            entry = self._mapping.get(key, {})
            label = entry.get("friendly") or entry.get("full_label") or entry.get("label") or self._fallback_label(key)
            category = entry.get("category") or None
            group = entry.get("group")
            section = entry.get("section")
            friendly_value = self._friendly_value(value, entry)

            if category:
                has_category = True

            container = self._ensure_container(structured, category, group, section)
            container[label] = friendly_value

        if has_category:
            result: Dict[str, Any] = {}
            for category in self._ordered_categories(structured):
                if category is None:
                    continue
                result[category] = self._sort_nested(structured[category])
            if None in structured and structured[None]:
                result['Other'] = self._sort_nested(structured[None])
            return result

        return self._sort_nested(structured.get(None, {}))

    def _ensure_container(
        self,
        structured: Dict[Optional[str], Dict[str, Any]],
        category: Optional[str],
        group: Optional[str],
        section: Optional[str],
    ) -> Dict[str, Any]:
        cat_key = category or None
        bucket = structured.setdefault(cat_key, {})
        container: Dict[str, Any] = bucket
        if group:
            container = bucket.setdefault(group, {})
        if section:
            container = container.setdefault(section, {})
        return container

    def _ordered_categories(self, structured: Dict[Optional[str], Any]) -> list[Optional[str]]:
        keys = list(structured.keys())
        keys.sort(key=lambda cat: (
            1 if cat is None else 0,
            self._category_index.get(cat, len(self._category_index)),
            cat or "",
        ))
        return keys

    def _sort_nested(self, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        return {key: self._sort_nested(value) for key, value in sorted(data.items())}

    def _friendly_value(self, value: Any, entry: Dict[str, Any]) -> Any:
        enum_map = entry.get("enum_map")
        sidetext = entry.get("sidetext")

        def add_unit(res: Any) -> Any:
            if not sidetext or res in (None, "", "nil"):
                return res
            text = str(res)
            if sidetext in text or '%' in text:
                return text
            return f"{text} {sidetext}"

        if not enum_map:
            if isinstance(value, list):
                return [add_unit(item) for item in value]
            return add_unit(value)

        def translate(item: Any) -> Any:
            key = str(item)
            label = enum_map.get(key) or enum_map.get(item)
            if not label:
                return item
            return f"{label} ({item})" if label != key else label

        if isinstance(value, list):
            return [add_unit(translate(item)) for item in value]
        return add_unit(translate(value))

    @staticmethod
    def _fallback_label(key: str) -> str:
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


@lru_cache(maxsize=1)
def get_label_mapper() -> LabelMapper:
    return LabelMapper()


@lru_cache(maxsize=1)
def get_label_metadata() -> Dict[str, Dict[str, Any]]:
    data = resources.files(__package__).joinpath("data/labels.json").read_text("utf-8")
    return json.loads(data)


def get_scope_for_key(key: str) -> Optional[Any]:
    metadata = get_label_metadata().get(key)
    if not metadata:
        return None
    scope = metadata.get("scope")
    if isinstance(scope, list):
        return scope.copy()
    return scope
