"""Telegram notifications (falls back to log only)."""
from __future__ import annotations

import logging
import threading

import requests

log = logging.getLogger("notify")


class Notifier:
    def __init__(self, enabled: bool, token: str, chat_id: str):
        self.enabled = bool(enabled and token and chat_id)
        self.token = token
        self.chat_id = chat_id
        if enabled and not self.enabled:
            log.warning("Telegram enabled but TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID missing - alerts go to log only")

    def send(self, text: str) -> None:
        log.info("ALERT | %s", text.replace("\n", " | "))
        if not self.enabled:
            return
        threading.Thread(target=self._post, args=(text,), daemon=True).start()

    def _post(self, text: str) -> None:
        try:
            requests.post(f"https://api.telegram.org/bot{self.token}/sendMessage",
                          json={"chat_id": self.chat_id, "text": text}, timeout=10)
        except Exception as e:  # noqa
            log.warning("telegram failed: %s", e)
