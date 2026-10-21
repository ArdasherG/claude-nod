#!/usr/bin/env python3
"""Хук Claude Code на событие Stop: сообщение «Claude закончил» в Telegram.

Ничего не печатает на выход и всегда завершается с кодом 0 — на работу
Claude Code не влияет никак. Настройки берутся из того же .env.
"""
import html
import json
import os
import sys
import time
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
MAX_ANSWER = 700        # сколько символов ответа показывать
DEBUG = False


def debug(msg):
    if DEBUG:
        sys.stderr.write("[tg_notify] %s\n" % msg)


def read_env():
    data = {}
    try:
        path = HERE / ".env"
        if path.exists():
            for line in path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                data[key.strip()] = value.strip()
    except Exception as exc:
        debug("не удалось прочитать .env: %s" % exc)
    for key in ("TG_TOKEN", "TG_CHAT_ID", "CC_TG_DEBUG"):
        if os.environ.get(key):
            data[key] = os.environ[key].strip()
    return data


def last_answer(transcript_path):
    """Последний текстовый ответ Claude из файла расшифровки разговора."""
    try:
        path = Path(transcript_path)
        if not path.exists():
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
                if isinstance(content, str):
                    text = content
                elif isinstance(content, list):
                    parts = [
                        block.get("text", "")
                        for block in content
                        if isinstance(block, dict) and block.get("type") == "text"
                    ]
                    joined = "\n".join(p for p in parts if p).strip()
                    if joined:
                        text = joined
        return text
    except Exception as exc:
        debug("не удалось прочитать расшифровку: %s" % exc)
        return ""


def shorten(text, limit=MAX_ANSWER):
    """Начало ответа, обрезанное по границе строки или слова."""
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    cut = text[:limit]
    for sep in ("\n", " "):
        pos = cut.rfind(sep)
        if pos > limit * 0.6:        # рвём по границе, но не слишком рано
            cut = cut[:pos]
            break
    return cut.rstrip() + " …"


def main():
    global DEBUG

    raw = sys.stdin.read()
    payload = json.loads(raw) if raw and raw.strip() else {}
    if not isinstance(payload, dict):
        return

    env = read_env()
    DEBUG = env.get("CC_TG_DEBUG", "0") not in ("0", "", "false", "no")
    token = env.get("TG_TOKEN", "")
    chat_id = env.get("TG_CHAT_ID", "")
    if not token or not chat_id:
        return

    cwd = str(payload.get("cwd") or "")
    project = os.path.basename(cwd.rstrip("\\/")) or cwd or "проект"
    answer = shorten(last_answer(payload.get("transcript_path") or ""))

    rows = ["✅ <b>%s</b> — Claude закончил в %s"
            % (html.escape(project), time.strftime("%H:%M"))]
    if answer:
        rows.append("<blockquote>%s</blockquote>" % html.escape(answer))

    body = json.dumps({
        "chat_id": chat_id,
        "text": "\n".join(rows),
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
        "disable_notification": False,
    }).encode("utf-8")
    req = urllib.request.Request(
        "https://api.telegram.org/bot%s/sendMessage" % token,
        data=body, headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        resp.read()


if __name__ == "__main__":
    try:
        main()
    except BaseException as exc:          # молчать обязаны при любой ошибке
        debug("ошибка: %r" % exc)
    sys.exit(0)
