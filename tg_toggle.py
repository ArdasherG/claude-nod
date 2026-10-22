#!/usr/bin/env python3
"""Включает и выключает телеграм-бота одной командой.

    python tg_toggle.py on       ухожу от компьютера — вопросы в телефон
    python tg_toggle.py off      работаю за компьютером — вопросы в терминал
    python tg_toggle.py          показать, как сейчас

То же самое можно сделать с телефона командами /on, /off и /status —
их понимает сам бот. Правится одна строка CC_TG_ENABLED в .env,
перезапускать Claude Code не нужно: настройки читаются при каждом запросе.
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import tg_common as tg                                            # noqa: E402

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


def show(env):
    on = tg.enabled(env)
    print("  бот: %s" % ("✅ включён — вопросы идут в Telegram"
                         if on else "🔕 выключен — вопросы идут в терминал"))
    if not env.get("TG_TOKEN") or not env.get("TG_CHAT_ID"):
        print("  внимание: в .env не заполнены TG_TOKEN или TG_CHAT_ID")
    quiet = env.get("CC_TG_QUIET")
    print("  тихие часы: %s" % (quiet or "не заданы"))
    print("  ответ с телефона: %s" % (
        "включён" if tg.flag(env, "CC_TG_FOLLOWUP") else "выключен"))
    return on


def main():
    word = (sys.argv[1].lower() if len(sys.argv) > 1 else "").lstrip("-")
    env = tg.read_env()

    if word in ("", "status", "state"):
        show(env)
        return 0

    if word in ("on", "вкл", "1", "true", "yes"):
        want = True
    elif word in ("off", "выкл", "0", "false", "no"):
        want = False
    else:
        print("Не понял «%s». Ожидаю: on, off или ничего." % word)
        return 1

    if not tg.set_enabled(want):
        print("  не удалось записать .env — проверьте права на файл")
        return 1

    env = tg.read_env()
    show(env)

    # Сообщить в чат, что состояние изменилось, — чтобы это было видно и там.
    token, chat_id = env.get("TG_TOKEN"), env.get("TG_CHAT_ID")
    if token and chat_id:
        text = ("✅ Включён с компьютера — вопросы приходят сюда."
                if want else "🔕 Выключен с компьютера.\nВернуть: /on")
        try:
            tg.send(token, chat_id, text, quiet=True)
        except tg.TgFail as exc:
            print("  (в Telegram сообщить не удалось: %s)" % exc)
    return 0


if __name__ == "__main__":
    sys.exit(main())
