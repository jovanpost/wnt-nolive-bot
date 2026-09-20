"""Telegram: send messages, and (optionally) answer a few /nolive_* commands.

If you reuse the gap bot's Telegram bot, keep TELEGRAM_COMMANDS=false: two apps must not both listen
on one bot token. Sending messages from two apps with the same token is fine.
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Callable

import requests

from . import config as C

log = logging.getLogger("nolive.notify")

API = "https://api.telegram.org"
_handlers: dict = {}
_listener_started = False


def _url(method: str) -> str:
    return "%s/bot%s/%s" % (API, C.TELEGRAM_TOKEN, method)


def configured() -> bool:
    return bool(C.TELEGRAM_TOKEN and C.TELEGRAM_CHAT_ID)


def send(text: str, quiet: bool = False):
    if not configured():
        log.info("telegram not configured; message not sent: %s", text[:80])
        return None
    payload = {"chat_id": C.TELEGRAM_CHAT_ID, "text": text[:4000],
               "disable_web_page_preview": True, "disable_notification": quiet}
    try:
        resp = requests.post(_url("sendMessage"), json=payload, timeout=30)
    except requests.RequestException as exc:
        log.error("telegram send failed: %s", exc)
        return None
    if resp.status_code != 200:
        log.error("telegram send %s: %s", resp.status_code, resp.text[:200])
        return None
    try:
        return int((resp.json().get("result") or {}).get("message_id"))
    except Exception:
        return None


def register(command: str, handler: Callable) -> None:
    _handlers[command.lower().lstrip("/")] = handler


def _allowed(chat_id) -> bool:
    return str(chat_id) == str(C.TELEGRAM_CHAT_ID)


def _listen_loop() -> None:
    offset = None
    while True:
        try:
            params = {"timeout": 30, "allowed_updates": ["message"]}
            if offset is not None:
                params["offset"] = offset
            resp = requests.get(_url("getUpdates"), params=params, timeout=40)
            if resp.status_code == 409:      # someone else is listening on this token
                log.warning("telegram 409: another app is listening on this bot token")
                time.sleep(60)
                continue
            if resp.status_code != 200:
                time.sleep(5)
                continue
            for upd in resp.json().get("result", []):
                offset = int(upd["update_id"]) + 1
                msg = upd.get("message") or {}
                if not _allowed((msg.get("chat") or {}).get("id")):
                    continue
                text = (msg.get("text") or "").strip()
                if not text.startswith("/"):
                    continue
                parts = text.split()
                cmd = parts[0].split("@")[0].lower().lstrip("/")
                handler = _handlers.get(cmd)
                if handler is None:
                    continue
                try:
                    reply = handler(parts[1:])
                except Exception as exc:
                    reply = "error: %s" % exc
                if reply:
                    send(reply)
        except Exception:
            log.exception("telegram listener")
            time.sleep(10)


def start_listener() -> None:
    global _listener_started
    if _listener_started or not C.TELEGRAM_COMMANDS or not configured():
        return
    _listener_started = True
    threading.Thread(target=_listen_loop, name="nolive-telegram", daemon=True).start()
