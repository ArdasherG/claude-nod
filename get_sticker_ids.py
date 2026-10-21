#!/usr/bin/env python3
"""Помощник: раскладывает присланные боту стикеры по событиям в stickers.json.

Как пользоваться:
  1. Соберите свой стикер-пак через @Stickers в Telegram.
  2. Запустите:  python get_sticker_ids.py --assign
  3. Скрипт по очереди просит стикер для каждого события — просто присылайте
     их боту в чат. После каждого присланного файл сразу сохраняется.

Другие режимы:
  python get_sticker_ids.py              показать, что приходит (file_id)
  python get_sticker_ids.py --list       показать текущую карту
  python get_sticker_ids.py --assign allowed denied timeout
                                         заполнить только эти события
  python get_sticker_ids.py --clear done очистить одно событие
"""
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import tg_common as tg                                            # noqa: E402

WAIT_TOTAL = 600        # сколько всего ждём стикеры, секунд
POLL = 20

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


def load_raw():
    try:
        data = json.loads(tg.STICKERS_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_raw(data):
    ordered = {"_подсказка": data.get(
        "_подсказка",
        "file_id стикеров. Заполняется через get_sticker_ids.py --assign")}
    for name, _kind in tg.STICKER_EVENTS:
        ordered[name] = data.get(name, "")
    tg.STICKERS_PATH.write_text(
        json.dumps(ordered, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def show_map():
    data = load_raw()
    filled = 0
    for name, kind in tg.STICKER_EVENTS:
        value = str(data.get(name) or "")
        filled += bool(value)
        print("  %-18s %-9s %s" % (name, kind, value[:28] + "…" if value else "—"))
    print("\n  заполнено %d из %d" % (filled, len(tg.STICKER_EVENTS)))


def poll_stickers(token, chat_id, deadline):
    """Генератор: отдаёт стикеры, которые владелец присылает боту."""
    while time.time() < deadline:
        if not tg.take_lock():
            print("  …ждёт другой процесс (видимо, висит запрос разрешения)")
            time.sleep(2)
            continue
        try:
            offset = tg.read_offset()
            params = {"allowed_updates": ["message"],
                      "offset": offset if offset is not None else -1,
                      "timeout": POLL if offset is not None else 0}
            try:
                updates = tg.api(token, "getUpdates", params,
                                 timeout=params["timeout"] + 20) or []
            except tg.TgFail as exc:
                print("  Telegram: %s" % exc)
                time.sleep(2)
                continue
            for update in updates:
                if "update_id" in update:
                    tg.write_offset(update["update_id"] + 1)
                message = update.get("message") or {}
                if str((message.get("from") or {}).get("id") or "") != str(chat_id):
                    continue
                sticker = message.get("sticker")
                if sticker and sticker.get("file_id"):
                    yield sticker
        finally:
            tg.drop_lock()


def upload_folder(token, chat_id, folder, only=None):
    """Отправляет боту файлы <событие>.png и запоминает выданные file_id.

    Так можно обойтись без сборки пака через @Stickers: Telegram принимает
    PNG 512×512 прямо в sendSticker и сам возвращает идентификатор.
    """
    if not os.path.isdir(folder):
        print("Папки %s нет." % folder)
        return 1
    data = load_raw()
    done = skipped = 0
    for name, _kind in tg.STICKER_EVENTS:
        if only and name not in only:
            continue
        path = os.path.join(folder, name + ".png")
        if not os.path.isfile(path):
            print("  %-18s — файла нет, пропускаю" % name)
            skipped += 1
            continue
        blob = open(path, "rb").read()
        if len(blob) > 512 * 1024:
            print("  %-18s — %d КБ, больше предела Telegram в 512 КБ"
                  % (name, len(blob) // 1024))
            skipped += 1
            continue
        try:
            result = tg.upload(token, "sendSticker",
                               {"chat_id": chat_id, "disable_notification": "true"},
                               "sticker", name + ".png", blob, "image/png")
        except tg.TgFail as exc:
            print("  %-18s — Telegram отказал: %s" % (name, exc))
            skipped += 1
            continue
        file_id = (result.get("sticker") or {}).get("file_id")
        if not file_id:
            print("  %-18s — ответ без file_id" % name)
            skipped += 1
            continue
        data[name] = file_id
        save_raw(data)
        done += 1
        print("  %-18s готов  %s…" % (name, file_id[:24]))
    print("\nЗагружено %d, пропущено %d. stickers.json сохранён." % (done, skipped))
    return 0


def main():
    args = [a for a in sys.argv[1:]]
    env = tg.read_env()
    token = env.get("TG_TOKEN", "")
    chat_id = env.get("TG_CHAT_ID", "")

    if "--list" in args:
        show_map()
        return 0

    if "--clear" in args:
        data = load_raw()
        names = [a for a in args if not a.startswith("--")]
        for name in names or [n for n, _ in tg.STICKER_EVENTS]:
            data[name] = ""
        save_raw(data)
        print("Очищено: %s" % (", ".join(names) if names else "всё"))
        return 0

    if not token or not chat_id:
        print("В .env не заполнены TG_TOKEN и/или TG_CHAT_ID.")
        return 1

    if "--upload" in args:
        plain = [a for a in args if not a.startswith("--")]
        folder = plain[0] if plain and os.path.isdir(plain[0]) else "stiker_pack/ready"
        only = [a for a in plain if a != folder]
        return upload_folder(token, chat_id, folder, only)

    tg.prepare_state_dir()
    assign = "--assign" in args
    wanted = [a for a in args if not a.startswith("--")]
    if assign:
        data = load_raw()
        if not wanted:
            wanted = [n for n, _ in tg.STICKER_EVENTS if not data.get(n)]
        if not wanted:
            print("Все события уже заполнены. Для замены: --clear <событие>")
            show_map()
            return 0
        print("Заполняем %d событий. Присылайте стикеры боту по одному.\n"
              "Прервать — Ctrl+C.\n" % len(wanted))
        print("  следующий: %s  (1/%d)" % (wanted[0], len(wanted)))
        index = 0
        for sticker in poll_stickers(token, chat_id, time.time() + WAIT_TOTAL):
            name = wanted[index]
            data[name] = sticker["file_id"]
            save_raw(data)
            print("  %-18s <- %s %s" % (name, sticker.get("emoji", ""),
                                        sticker["file_id"][:24] + "…"))
            index += 1
            if index >= len(wanted):
                print("\nГотово, stickers.json сохранён.")
                show_map()
                return 0
            print("  следующий: %s  (%d/%d)"
                  % (wanted[index], index + 1, len(wanted)))
        print("\nВремя вышло. Заполнено %d из %d, остальное можно добить "
              "повторным запуском." % (index, len(wanted)))
        return 0

    print("Присылайте стикеры боту — покажу их file_id. Ctrl+C для выхода.\n")
    for sticker in poll_stickers(token, chat_id, time.time() + WAIT_TOTAL):
        print("  %s  набор=%s  file_id=%s"
              % (sticker.get("emoji", "?"), sticker.get("set_name", "?"),
                 sticker["file_id"]))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\nПрервано.")
        tg.drop_lock()
        sys.exit(0)
