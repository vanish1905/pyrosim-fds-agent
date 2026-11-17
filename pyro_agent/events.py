from __future__ import annotations

import json
import threading
from datetime import datetime

from .config import case_root

_WRITE_LOCK = threading.Lock()
EVENT_FILE_NAME = ".pyro_agent_events.jsonl"


def event_path(config: dict):
    return case_root(config) / EVENT_FILE_NAME


def emit_event(config: dict, event_type: str, message: str, **data) -> None:
    """Append a small UI event without allowing a telemetry failure to break MCP work."""
    event = {
        "timestamp": datetime.now().astimezone().isoformat(timespec="milliseconds"),
        "type": event_type,
        "message": message,
        "data": data,
    }
    try:
        path = event_path(config)
        encoded = json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n"
        with _WRITE_LOCK, path.open("a", encoding="utf-8") as stream:
            stream.write(encoded)
            stream.flush()
    except (OSError, KeyError, TypeError, ValueError):
        # The MCP result and solver must remain usable even if the companion is closed.
        return
