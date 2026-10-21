#!/usr/bin/env python3
"""Помощник: показывает chat_id тех, кто писал боту.

Как пользоваться:
  1. Найдите своего бота в Telegram и отправьте ему /start (или любое сообщение).
  2. Запустите этот файл.
  3. Скопируйте показанный chat_id в .env, в строку TG_CHAT_ID=
"""
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


def read_env():
    """Читает .env рядом со скриптом: строки вида КЛЮЧ=значение."""
    data = {}
    path = HERE / ".env"
    if not path.exists():
        return data
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        data[key.strip()] = value.strip()
    return data


def main():
    token = read_env().get("TG_TOKEN", "")
    if not token:
        print("В .env не заполнен TG_TOKEN.")
        return 1

    url = "https://api.telegram.org/bot%s/getUpdates?timeout=0&limit=100" % token
    try:
        with urllib.request.urlopen(url, timeout=20) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        print("Telegram ответил ошибкой %s. Скорее всего, неверный токен." % exc.code)
        return 1
    except Exception as exc:
        print("Не удалось связаться с Telegram: %s" % exc)
        return 1

    if not body.get("ok"):
        print("Telegram ответил: %s" % body.get("description", "неизвестная ошибка"))
        return 1

    found = {}
    for upd in body.get("result", []):
        for key in ("message", "edited_message", "channel_post", "callback_query"):
            obj = upd.get(key)
            if not obj:
                continue
            who = obj.get("from") or {}
            chat = obj.get("message", {}).get("chat") if key == "callback_query" else obj.get("chat")
            chat_id = (chat or {}).get("id") or who.get("id")
            if chat_id is None:
                continue
            name = " ".join(x for x in (who.get("first_name"), who.get("last_name")) if x)
            username = who.get("username")
            found[chat_id] = "%s%s" % (name or "без имени", " (@%s)" % username if username else "")

    if not found:
        print("Новых сообщений нет.")
        print("Откройте бота в Telegram, отправьте ему /start и запустите этот файл снова.")
        print("Если вы уже писали боту давно — отправьте любое новое сообщение:")
        print("Telegram отдаёт только свежие события.")
        return 1

    print("Найдено:")
    for chat_id, who in found.items():
        print("  chat_id = %s   %s" % (chat_id, who))
    print()
    print("Впишите нужный chat_id в .env:  TG_CHAT_ID=%s" % next(iter(found)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
