#!/usr/bin/env python3
"""Хук Claude Code на событие Stop: присылает ответ в Telegram.

Две роли:
  1. Уведомление «Claude закончил» с текстом ответа целиком (длинный ответ
     разбивается на несколько сообщений).
  2. Необязательный двусторонний канал: если в .env включено CC_TG_FOLLOWUP=1,
     скрипт ждёт текстовый ответ из Telegram и возвращает его Claude Code как
     причину не завершать ход. Тогда написанное с телефона становится
     следующей инструкцией.

Без ответа и при любой ошибке скрипт печатает пустоту и выходит с кодом 0 —
Claude Code завершает ход как обычно.
"""
import json
import os
import sys
import threading
import time
import uuid

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import tg_common as tg                                            # noqa: E402

HOOK_EVENT = "Stop"
ANSWER_LIMIT = 10000    # сколько символов ответа отправлять (0 = без предела)
FOLLOWUP_WAIT = 90      # сколько секунд ждать ответ с телефона
FOLLOWUP_MAX = 3        # сколько продолжений подряд разрешаем
WATCHDOG_EXTRA = 20


def counter_path(session_id):
    return tg.STATE_DIR / ("followup-%s.json" % tg.session_path(session_id).stem)


def followup_count(session_id):
    try:
        return int(json.loads(
            counter_path(session_id).read_text(encoding="utf-8")).get("count", 0))
    except Exception:
        return 0


def set_followup_count(session_id, value):
    try:
        tg.STATE_DIR.mkdir(parents=True, exist_ok=True)
        counter_path(session_id).write_text(
            json.dumps({"count": int(value), "ts": time.time()}), encoding="utf-8")
    except Exception as exc:
        tg.debug("счётчик продолжений не записан: %s" % exc)


def last_answer(payload):
    """Текст последнего ответа Claude.

    Документация советует брать last_assistant_message: файл расшифровки
    пишется асинхронно и может не содержать свежих сообщений текущего хода.
    Чтение файла оставлено как запасной путь для старых версий.
    """
    direct = payload.get("last_assistant_message")
    if isinstance(direct, str) and direct.strip():
        return direct.strip()
    if isinstance(direct, dict):
        content = direct.get("content")
        if isinstance(content, str) and content.strip():
            return content.strip()
        if isinstance(content, list):
            parts = [b.get("text", "") for b in content
                     if isinstance(b, dict) and b.get("type") == "text"]
            joined = "\n".join(p for p in parts if p).strip()
            if joined:
                return joined
    return from_transcript(payload.get("transcript_path") or "")


def from_transcript(path):
    """Последний текстовый ответ Claude из файла расшифровки разговора."""
    try:
        if not path or not os.path.exists(path):
            return ""
        text = ""
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line or '"assistant"' not in line:
                    continue
                try:
                    entry = json.loads(line)
                except ValueError:
                    continue
                if entry.get("type") != "assistant":
                    continue
                content = (entry.get("message") or {}).get("content")
                if isinstance(content, str) and content.strip():
                    text = content.strip()
                elif isinstance(content, list):
                    parts = [b.get("text", "") for b in content
                             if isinstance(b, dict) and b.get("type") == "text"]
                    joined = "\n".join(p for p in parts if p).strip()
                    if joined:
                        text = joined
        return text
    except Exception as exc:
        tg.debug("расшифровка не прочитана: %s" % exc)
        return ""


def emit_continue(reason):
    """Просит Claude Code не завершать ход, а продолжить с этой инструкцией."""
    out = {
        "decision": "block",
        "reason": reason,
        "systemMessage": "Из Telegram: %s" % tg.clip(reason, 200),
    }
    sys.stdout.write(json.dumps(out))
    sys.stdout.flush()


def main():
    raw = sys.stdin.read()
    payload = json.loads(raw) if raw and raw.strip() else {}
    if not isinstance(payload, dict):
        return

    env = tg.read_env()
    token = env.get("TG_TOKEN", "")
    chat_id = env.get("TG_CHAT_ID", "")
    if not token or not chat_id:
        return

    quiet = tg.quiet_now(env.get("CC_TG_QUIET"))
    followup_on = tg.flag(env, "CC_TG_FOLLOWUP", False) and not quiet
    wait_for = tg.number(env, "CC_TG_FOLLOWUP_WAIT", FOLLOWUP_WAIT, low=10, high=600)
    max_followups = tg.number(env, "CC_TG_FOLLOWUP_MAX", FOLLOWUP_MAX, low=0, high=20)
    limit = tg.number(env, "CC_TG_ANSWER_LIMIT", ANSWER_LIMIT, low=0, high=40000)

    threading.Thread(
        target=lambda: (time.sleep((wait_for if followup_on else 15) + WATCHDOG_EXTRA),
                        os._exit(0)),
        daemon=True,
    ).start()

    session_id = payload.get("session_id")
    cwd = str(payload.get("cwd") or "")
    project = os.path.basename(cwd.rstrip("\\/")) or cwd or "проект"
    answer = last_answer(payload)
    if limit and len(answer) > limit:
        answer = tg.shorten(answer, limit)

    done = followup_count(session_id)
    can_ask = followup_on and done < max_followups

    head = "✅ <b>%s</b> — Claude закончил в %s" % (
        tg.esc(project), time.strftime("%H:%M"))
    body = tg.md_to_html(answer) if answer else "<i>ответ пустой</i>"
    tail, keyboard = "", None
    rid = uuid.uuid4().hex[:8]
    if can_ask:
        tail = ("\n\n💬 <b>Ответьте на это сообщение</b>, чтобы Claude продолжил "
                "(жду %d с). Продолжение %d из %d."
                % (wait_for, done + 1, max_followups))
        # Кнопка нужна, чтобы длинное окно ожидания ничего не стоило: если
        # продолжать не надо, одно нажатие — и ход завершается сразу.
        keyboard = [[{"text": "⏹ Не жди меня", "callback_data": "n:%s" % rid}]]

    stickers = tg.load_stickers()
    smode = tg.sticker_mode(env, stickers)
    tg.sticker(token, chat_id, "waiting_reply" if can_ask else "done",
               smode, quiet=quiet, stickers=stickers)

    try:
        sent = tg.send_parts(token, chat_id, head + "\n" + body + tail,
                             keyboard=keyboard, quiet=quiet)
    except tg.TgFail as exc:
        tg.debug("уведомление не отправлено: %s" % exc)
        tg.log({"tool": HOOK_EVENT, "session": session_id, "via": "notify_failed",
                "error": str(exc)})
        return

    if not can_ask:
        tg.log({"tool": HOOK_EVENT, "session": session_id, "cwd": cwd,
                "via": "notify", "chars": len(answer)})
        return

    # Ждём текст с телефона. Петли здесь быть не может: продолжение случается
    # только если человек действительно что-то написал.
    message_id = (sent or {}).get("message_id")
    tg.prepare_state_dir()
    kind, data = tg.wait(token, chat_id, time.time() + wait_for,
                         rid=rid, want_text=True, reply_to=message_id)
    if kind == "press":
        # Нажали «Не жди меня» — завершаем ход сразу, не тратя окно впустую.
        tg.answer_press(token, data.get("callback_id"), "Хорошо, не жду")
        tg.edit(token, data.get("chat_id") or chat_id, data.get("message_id"),
                head + "\n" + body + "\n\n⏹ <i>Продолжение не нужно</i>")
        set_followup_count(session_id, 0)
        tg.log({"tool": HOOK_EVENT, "session": session_id, "cwd": cwd,
                "via": "notify_dismissed", "chars": len(answer)})
        return
    if kind != "text":
        set_followup_count(session_id, 0)          # пауза — бюджет обновляется
        tg.log({"tool": HOOK_EVENT, "session": session_id, "cwd": cwd,
                "via": "notify_no_reply", "chars": len(answer)})
        return

    reply = tg.clip((data.get("text") or "").strip(), 2000)
    set_followup_count(session_id, done + 1)
    tg.sticker(token, chat_id, "followup_sent", smode, quiet=True, stickers=stickers)
    try:
        tg.send(token, chat_id, "▶️ <i>Передал Claude:</i>\n%s" % tg.esc(reply),
                quiet=True)
    except tg.TgFail:
        pass
    tg.log({"tool": HOOK_EVENT, "session": session_id, "cwd": cwd,
            "via": "followup", "reply": tg.clip(reply, 200), "step": done + 1})
    emit_continue(reply)


if __name__ == "__main__":
    try:
        main()
    except BaseException as exc:          # молчать обязаны при любой ошибке
        tg.debug("ошибка: %r" % exc)
        try:
            tg.log({"tool": HOOK_EVENT, "via": "error", "error": repr(exc)})
        except Exception:
            pass
    sys.exit(0)
