#!/usr/bin/env python3
"""Хук Claude Code на событие SessionStart: проверка, что одобрения будут работать.

Один раз при старте сессии проверяет настройки и доступность бота и,
только если что-то не так, печатает предупреждение в терминал. Когда всё
в порядке — молчит, чтобы не мешать.

Главная ловушка, из-за которой одобрения «не работают»: режим разрешений,
который ничего не спрашивает (auto, bypassPermissions, dontAsk). В таком
режиме событие PermissionRequest не возникает вообще, и хук не вызывается.
"""
import json
import os
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import tg_common as tg                                            # noqa: E402

HOOK_EVENT = "SessionStart"
SILENT_MODES = ("auto", "bypasspermissions", "dontask")
CHECK_TIMEOUT = 8

MODE_STICKERS = {
    "default": "mode_default",
    "plan": "mode_plan",
    "acceptedits": "mode_accept_edits",
    "auto": "mode_auto",
    "dontask": "mode_auto",
    "bypasspermissions": "mode_bypass",
}


def problems(env, payload):
    """Список замечаний. Пустой список означает «всё в порядке»."""
    notes = []
    token = env.get("TG_TOKEN", "")
    chat_id = env.get("TG_CHAT_ID", "")

    if not token and not chat_id:
        notes.append("не заполнены TG_TOKEN и TG_CHAT_ID в .env — "
                     "одобрения через Telegram выключены")
        return notes
    if not token:
        notes.append("в .env не заполнен TG_TOKEN")
    if not chat_id:
        notes.append("в .env не заполнен TG_CHAT_ID — запустите get_chat_id.py")

    mode = str(payload.get("permission_mode") or "").strip().lower()
    if mode in SILENT_MODES:
        notes.append("режим разрешений «%s» ничего не спрашивает, поэтому "
                     "запросы в Telegram приходить не будут: переключитесь на "
                     "default через Shift+Tab" % mode)

    if token and chat_id:
        try:
            who = tg.api(token, "getMe", {}, timeout=CHECK_TIMEOUT) or {}
            tg.debug("бот доступен: @%s" % who.get("username"))
        except tg.TgFail as exc:
            notes.append("бот недоступен (%s) — запросы уйдут в терминал" % exc)
    return notes


def main():
    raw = sys.stdin.read()
    payload = json.loads(raw) if raw and raw.strip() else {}
    if not isinstance(payload, dict):
        payload = {}

    threading.Thread(
        target=lambda: (time.sleep(CHECK_TIMEOUT + 10), os._exit(0)),
        daemon=True,
    ).start()

    env = tg.read_env()
    notes = problems(env, payload)
    token = env.get("TG_TOKEN", "")
    chat_id = env.get("TG_CHAT_ID", "")

    # Стикер режима: сразу видно, будут ли вообще приходить вопросы.
    if token and chat_id:
        stickers = tg.load_stickers()
        smode = tg.sticker_mode(env, stickers)
        mode = str(payload.get("permission_mode") or "").strip().lower()
        name = MODE_STICKERS.get(mode, "session_start")
        if tg.quiet_now(env.get("CC_TG_QUIET")):
            name = "quiet_hours"
        elif notes and name == "session_start":
            name = "error"
        tg.sticker(token, chat_id, name, smode, quiet=True, stickers=stickers)

    if not notes:
        return

    tg.log({"tool": HOOK_EVENT, "session": payload.get("session_id"),
            "via": "session_check", "notes": notes})
    message = "Telegram-одобрения: " + "; ".join(notes)
    sys.stdout.write(json.dumps({"systemMessage": message}))
    sys.stdout.flush()


if __name__ == "__main__":
    try:
        main()
    except BaseException as exc:          # молчать обязаны при любой ошибке
        tg.debug("ошибка: %r" % exc)
    sys.exit(0)
