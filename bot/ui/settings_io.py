"""Read / write config/settings.yaml from the dashboard while keeping its comments and layout."""
from __future__ import annotations

import json
import re
import shutil
from pathlib import Path
from typing import Any, Dict, List, Tuple

import yaml

from ..config import DEFAULT_SETTINGS, load_settings

LINE = re.compile(r"^(?P<ind>\s*)(?P<key>[A-Za-z0-9_]+):(?P<sp>[ \t]*)(?P<val>.*?)(?P<cmt>\s+#.*)?$")
GROUP = re.compile(r"^\s*#\s*===\s*(?P<name>.+?)\s*===\s*$")
YAML_WORDS = {"true", "false", "yes", "no", "on", "off", "null", "none", "~"}

Path_ = Tuple[str, ...]


def fmt_value(v: Any) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return repr(v)
    if isinstance(v, (list, tuple)):
        return "[" + ", ".join(fmt_value(x) for x in v) + "]"
    s = "" if v is None else str(v)
    if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", s) and s.lower() not in YAML_WORDS:
        return s
    return json.dumps(s)


def scan(text: str) -> Tuple[Dict[Path_, str], Dict[Path_, str]]:
    """-> ({path: inline comment}, {strategy path: Pine group name})"""
    comments: Dict[Path_, str] = {}
    groups: Dict[Path_, str] = {}
    stack: List[Tuple[int, str]] = []
    group = ""
    for line in text.splitlines():
        g = GROUP.match(line)
        if g:
            group = g.group("name")
            continue
        m = LINE.match(line)
        if not m or line.lstrip().startswith("#"):
            continue
        ind = len(m.group("ind"))
        while stack and stack[-1][0] >= ind:
            stack.pop()
        path = tuple(k for _, k in stack) + (m.group("key"),)
        if m.group("cmt"):
            comments[path] = m.group("cmt").strip().lstrip("#").strip()
        if path[0] == "strategy" and len(path) == 2:
            groups[path] = group
        if m.group("val").strip() == "":
            stack.append((ind, m.group("key")))
    return comments, groups


def update_text(text: str, updates: Dict[Path_, Any]) -> str:
    out = []
    stack: List[Tuple[int, str]] = []
    done = set()
    for line in text.splitlines(keepends=True):
        body = line.rstrip("\r\n")
        nl = line[len(body):]
        m = LINE.match(body)
        if m and not body.lstrip().startswith("#"):
            ind = len(m.group("ind"))
            while stack and stack[-1][0] >= ind:
                stack.pop()
            path = tuple(k for _, k in stack) + (m.group("key"),)
            if m.group("val").strip() == "":
                stack.append((ind, m.group("key")))
            elif path in updates:
                sp = m.group("sp") or " "
                body = f"{m.group('ind')}{m.group('key')}:{sp}{fmt_value(updates[path])}{m.group('cmt') or ''}"
                done.add(path)
        out.append(body + nl)
    missing = set(updates) - done
    if missing:
        raise KeyError(f"settings keys not found in YAML: {sorted(missing)}")
    return "".join(out)


def read_raw(path: Path = DEFAULT_SETTINGS) -> Tuple[dict, str]:
    text = Path(path).read_text(encoding="utf-8")
    return yaml.safe_load(text) or {}, text


def flatten(d: dict, prefix: Path_ = ()) -> Dict[Path_, Any]:
    out: Dict[Path_, Any] = {}
    for k, v in d.items():
        if isinstance(v, dict):
            out.update(flatten(v, prefix + (k,)))
        else:
            out[prefix + (k,)] = v
    return out


def save(updates: Dict[Path_, Any], path: Path = DEFAULT_SETTINGS) -> Path:
    """Write changed values, validate with the real loader, keep a backup. Raises on invalid settings."""
    path = Path(path)
    _, text = read_raw(path)
    new = update_text(text, updates)
    backup = path.with_name("settings.backup.yaml")
    shutil.copyfile(path, backup)
    path.write_text(new, encoding="utf-8")
    try:
        load_settings(path)
    except Exception:
        shutil.copyfile(backup, path)
        raise
    return backup
