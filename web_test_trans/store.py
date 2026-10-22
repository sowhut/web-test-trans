"""Atomic local persistence; one server process owns this directory."""
import hashlib
import json
import re
import threading
import time
import uuid
from pathlib import Path


def validate_locator(locator):
    if locator["backend"] == "web":
        for selector in [locator["css"], *locator.get("frames", [])]:
            # Selectors are generated as a list of actual DOM ancestors, never text.
            if re.search(r"(:has-text|:text|text=|xpath=|>>|:nth-|:first-child|:last-child|:first-of-type|:last-of-type|\[\s*\d+\s*\])", selector, re.I):
                raise ValueError("禁止文本、XPath 或顺序索引定位")
            if len(selector.split(" > ")) < 3:
                raise ValueError("CSS 定位至少需要三个真实 DOM 层级")
    elif locator["backend"] == "windows":
        path = locator["path"]
        if len(path) < 3 or any(not n.get("class_name") for n in path):
            raise ValueError("Windows 定位至少需要三层真实 ClassName")
        if any('index' in n or 'count' in n for n in path):
            raise ValueError("Windows 定位禁止顺序索引")
    else:
        raise ValueError("未知定位后端")


class Store:
    def __init__(self, root):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.registry_file = self.root / "components.json"
        self.components = self._read(self.registry_file, {})
        # Interrupted sessions remain readable and are explicitly marked.
        for file in self.root.glob("session_*.json"):
            value = self._read(file, {})
            if value.get("status") in ("recording", "starting", "stopping"):
                value["status"] = "interrupted"
                self._write(file, value)

    @staticmethod
    def _read(path, default):
        return json.loads(path.read_text("utf-8")) if path.exists() else default

    @staticmethod
    def _write(path, value):
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2), "utf-8")
        tmp.replace(path)

    def create(self, name, backend, **metadata):
        session = {"id": uuid.uuid4().hex, "name": name, "backend": backend,
                   "created_at": time.time(), "status": "starting", "events": [],
                   "warnings": [], **metadata}
        self.save(session)
        return session

    def save(self, session):
        with self.lock:
            self._write(self.root / f"session_{session['id']}.json", session)

    def get(self, session_id):
        if not re.fullmatch(r"[a-f0-9]{32}", session_id):
            raise KeyError(session_id)
        path = self.root / f"session_{session_id}.json"
        if not path.exists():
            raise KeyError(session_id)
        return self._read(path, {})

    def list(self):
        with self.lock:
            values = [self._read(p, {}) for p in self.root.glob("session_*.json")]
        return sorted([{k: v for k, v in s.items() if k != "events"} |
                       {"event_count": len(s["events"])} for s in values],
                      key=lambda s: s["created_at"], reverse=True)

    def register(self, kind, locator):
        validate_locator(locator)
        digest = hashlib.sha256(json.dumps([kind, locator], sort_keys=True).encode()).hexdigest()[:12]
        key = f"{kind}_{digest}"
        with self.lock:
            if key not in self.components:
                self.components[key] = {"key": key, "kind": kind, "locator": locator}
                self._write(self.registry_file, self.components)
        return key

    def append(self, session, event):
        with self.lock:
            event.setdefault("id", uuid.uuid4().hex)
            event.setdefault("at", time.time() * 1000)
            session["events"].append(event)
            self.save(session)
        return event

    def warn(self, session, message):
        with self.lock:
            if message not in session["warnings"]:
                session["warnings"].append(message)
                self.save(session)
