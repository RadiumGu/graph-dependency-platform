"""
registry/markdown_policy_parser.py — Parse Markdown policy files into policy dicts

Supports the structure defined in PRD §10.2:
  - ## headings → top-level keys
  - ### headings → second-level keys
  - 2-column tables → key-value dicts (auto type inference)
  - Multi-column tables → list of dicts
  - Unordered lists → string arrays
  - Blockquotes / prose → ignored (or sent to NL rule parser)
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Tuple


def parse_markdown_policy(text: str) -> Dict[str, Any]:
    """Parse a Markdown policy file into a structured policy dict.

    Args:
        text: Full Markdown text of the policy file.

    Returns:
        Nested dict matching the plan_policy.yaml schema.
    """
    result: Dict[str, Any] = {}
    current_h2 = ""
    current_h3 = ""
    current_h4 = ""
    lines = text.split("\n")
    i = 0

    while i < len(lines):
        line = lines[i].rstrip()

        # H1 — document title, skip
        if line.startswith("# ") and not line.startswith("## "):
            i += 1
            continue

        # H2 — top-level section
        if line.startswith("## "):
            current_h2 = _heading_to_key(line[3:].strip())
            current_h3 = ""
            current_h4 = ""
            i += 1
            continue

        # H3 — second-level section
        if line.startswith("### "):
            current_h3 = _heading_to_key(line[4:].strip())
            current_h4 = ""
            i += 1
            continue

        # H4 — third-level section
        if line.startswith("#### "):
            current_h4 = _heading_to_key(line[5:].strip())
            i += 1
            continue

        # Table detection
        if "|" in line and i + 1 < len(lines) and _is_separator(lines[i + 1]):
            headers, table_rows, consumed = _parse_table(lines, i)
            i += consumed

            if not current_h2:
                continue

            table_data = _table_to_data(headers, table_rows)

            # Place in the right location
            target = result.setdefault(current_h2, {})
            if current_h3:
                target = target.setdefault(current_h3, {})
            if current_h4:
                target = target.setdefault(current_h4, {})

            if isinstance(table_data, dict):
                target.update(table_data)
            elif isinstance(table_data, list):
                # For sub-section tables, store as list
                if current_h4:
                    result[current_h2][current_h3][current_h4] = table_data
                elif current_h3:
                    result[current_h2][current_h3] = table_data
                else:
                    result[current_h2] = table_data
            continue

        # Unordered list
        if line.startswith("- ") and current_h2:
            items, consumed = _parse_list(lines, i)
            i += consumed

            target = result.setdefault(current_h2, {})
            if current_h3:
                target = target.setdefault(current_h3, {})
            if current_h4:
                if isinstance(target, dict):
                    target[current_h4] = items
                continue

            # If we're at h2 or h3 level, store as list
            if current_h3:
                result[current_h2][current_h3] = items
            else:
                result[current_h2] = items
            continue

        i += 1

    # Map common Chinese headings to policy keys
    return _normalize_keys(result)


# ------------------------------------------------------------------
# Table parsing
# ------------------------------------------------------------------


def _is_separator(line: str) -> bool:
    """Check if a line is a Markdown table separator (|---|---|)."""
    stripped = line.strip()
    return bool(re.match(r"^\|[\s\-:|]+\|$", stripped))


def _parse_table(lines: List[str], start: int) -> Tuple[List[str], List[List[str]], int]:
    """Parse a Markdown table starting at `start`.

    Returns (headers, rows, lines_consumed).
    """
    header_line = lines[start].strip()
    headers = [h.strip() for h in header_line.split("|") if h.strip()]

    # Skip separator
    consumed = 2
    rows: List[List[str]] = []
    i = start + 2

    while i < len(lines):
        line = lines[i].strip()
        if not line or not line.startswith("|"):
            break
        cells = [c.strip() for c in line.split("|") if c.strip()]
        rows.append(cells)
        consumed += 1
        i += 1

    return headers, rows, consumed


def _table_to_data(headers: List[str], rows: List[List[str]]) -> Any:
    """Convert table to dict or list based on column count.

    2-column tables with first column as key → dict
    Multi-column tables → list of dicts
    """
    if len(headers) == 2:
        # Key-value dict
        result: Dict[str, Any] = {}
        key_header = headers[0].lower()
        val_header = headers[1].lower()

        for row in rows:
            if len(row) >= 2:
                key = _normalize_table_key(row[0])
                value = _infer_value(row[1])
                result[key] = value
        return result

    elif len(headers) >= 3:
        # Check if it's a "config | value | description" pattern
        lower_headers = [h.lower() for h in headers]
        if any(k in lower_headers for k in ("配置项", "配置", "key", "config")):
            # Still a key-value table, just with extra columns
            key_idx = 0
            val_idx = 1
            for idx, h in enumerate(lower_headers):
                if h in ("值", "value", "val"):
                    val_idx = idx
                    break

            result = {}
            for row in rows:
                if len(row) > val_idx:
                    key = _normalize_table_key(row[key_idx])
                    value = _infer_value(row[val_idx])
                    result[key] = value
            return result

        # Check if first column is a resource/item identifier (dict-of-dicts pattern)
        if any(k in lower_headers for k in ("资源类型", "resource_type", "type", "resource")):
            key_idx = 0
            for idx, h in enumerate(lower_headers):
                if h.lower() in ("资源类型", "resource_type", "type", "resource"):
                    key_idx = idx
                    break
            result = {}
            for row in rows:
                if len(row) > key_idx:
                    key = row[key_idx].strip()
                    entry: Dict[str, Any] = {}
                    for idx, header in enumerate(headers):
                        if idx != key_idx and idx < len(row):
                            entry[_normalize_table_key(header)] = _infer_value(row[idx])
                    result[key] = entry
            return result

        # Multi-column → list of dicts
        result_list: List[Dict[str, Any]] = []
        for row in rows:
            entry = {}
            for idx, header in enumerate(headers):
                if idx < len(row):
                    entry[_normalize_table_key(header)] = _infer_value(row[idx])
            result_list.append(entry)
        return result_list

    return {}


def _parse_list(lines: List[str], start: int) -> Tuple[List[str], int]:
    """Parse an unordered Markdown list."""
    items: List[str] = []
    consumed = 0
    i = start

    while i < len(lines):
        line = lines[i].rstrip()
        if line.startswith("- "):
            items.append(line[2:].strip())
            consumed += 1
            i += 1
        elif line.startswith("  ") and items:
            # Continuation line
            items[-1] += " " + line.strip()
            consumed += 1
            i += 1
        else:
            break

    return items, consumed


# ------------------------------------------------------------------
# Key normalization
# ------------------------------------------------------------------

_HEADING_MAP = {
    "通用策略": "general",
    "全局变量": "variables",
    "scope策略": "scope_policies",
    "scope 策略": "scope_policies",
    "phase策略": "phase_policies",
    "phase 策略": "phase_policies",
    "资源覆盖": "resource_overrides",
    "回滚策略": "rollback_policy",
    "通知": "notification",
    "不可自动回滚的操作": "non_reversible_actions",
    "通知渠道": "channels",
    "数据同步检查": "data_sync_checks",
    "执行上下文": "execution_contexts",
    "自定义规则": "custom_rules",
}


def _heading_to_key(heading: str) -> str:
    """Convert a heading to a dict key."""
    lower = heading.lower().strip()
    if lower in _HEADING_MAP:
        return _HEADING_MAP[lower]
    # Try case-insensitive match
    for zh, en in _HEADING_MAP.items():
        if zh.lower() == lower:
            return en
    # Keep hyphens for phase IDs (e.g., phase-1), only replace spaces
    return lower.replace(" ", "_")


def _normalize_table_key(key: str) -> str:
    """Normalize a table key (Chinese → English where possible)."""
    key_map = {
        "配置项": "key",
        "值": "value",
        "说明": "description",
        "变量名": "name",
        "资源类型": "resource_type",
        "检查条件": "check",
        "类型": "type",
        "配置": "config",
    }
    stripped = key.strip()
    return key_map.get(stripped, stripped.lower().replace(" ", "_"))


def _normalize_keys(data: Dict[str, Any]) -> Dict[str, Any]:
    """Apply key normalization to the top-level dict."""
    result: Dict[str, Any] = {}
    for key, value in data.items():
        mapped_key = _HEADING_MAP.get(key, key)
        result[mapped_key] = value

    # Nest 'variables' under 'general' if at top level
    if "variables" in result and "general" in result:
        if isinstance(result["general"], dict):
            result["general"]["variables"] = result.pop("variables")

    return result


def _infer_value(raw: str) -> Any:
    """Best-effort type inference for table cell values."""
    stripped = raw.strip()

    if stripped.lower() in ("true", "yes"):
        return True
    if stripped.lower() in ("false", "no"):
        return False
    try:
        return int(stripped)
    except ValueError:
        pass
    try:
        return float(stripped)
    except ValueError:
        pass

    # Keep ${...} references as strings
    return stripped
