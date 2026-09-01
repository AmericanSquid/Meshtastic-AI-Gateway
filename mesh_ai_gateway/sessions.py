from __future__ import annotations

from collections import defaultdict


class SessionStore:
    def __init__(self, history_messages: int):
        self.history_messages = history_messages
        self._sessions: dict[str, list[dict[str, str]]] = defaultdict(list)

    def get(self, key: str) -> list[dict[str, str]]:
        return list(self._sessions[key])

    def append(self, key: str, role: str, content: str) -> None:
        session = self._sessions[key]
        session.append({"role": role, "content": content})
        max_items = max(2, self.history_messages * 2)
        if len(session) > max_items:
            del session[:-max_items]

    def reset(self, key: str) -> None:
        self._sessions.pop(key, None)

    def reconfigure(self, history_messages: int) -> None:
        self.history_messages = history_messages
        max_items = max(2, history_messages * 2)
        for session in self._sessions.values():
            if len(session) > max_items:
                del session[:-max_items]
