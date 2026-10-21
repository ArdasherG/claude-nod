#!/usr/bin/env python3
"""Хук Claude Code на событие PermissionRequest: спрашивает разрешение в Telegram.

Как работает:
  Claude Code запускает этот скрипт и передаёт ему на вход (stdin) описание запроса.
  Скрипт отправляет сообщение в Telegram с кнопками и ждёт нажатия.
  Нажали «Разрешить» — печатает решение allow, «Запретить» — deny.
  Ничего не напечатал — Claude Code задаст обычный вопрос в терминале.

Главный принцип: любая ошибка и любой таймаут = пустой вывод и код выхода 0.
Claude Code не должен зависнуть или сломаться ни при каком раскладе.
"""
import html
import json
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

HERE = Path(__file__).resolve().parent
LOG_PATH = HERE / "decisions.jsonl"
STATE_DIR = Path(
    os.environ.get("TEMP") or os.environ.get("TMP") or "/tmp"
) / "cc-tg-approve"
LOCK_PATH = STATE_DIR / "poll.lock"
OFFSET_PATH = STATE_DIR / "offset.txt"

WAIT_TIMEOUT = 300      # сколько всего ждём нажатия, секунд
WATCHDOG_EXTRA = 20     # страховочный таймер = ожидание + столько
LONG_POLL = 20          # максимум секунд на один вопрос к Telegram
LOCK_STALE = 60         # через столько секунд брошенный флажок снимается
MAILBOX_TTL = 600       # через столько секунд чужие нажатия вычищаются
MAX_FIELD = 500         # обрезка команды/параметров в сообщении
MAX_TEXT = 3500         # обрезка всего сообщения (лимит Telegram — 4096)

DEBUG = False
HOOK_EVENT = "PermissionRequest"

FILE_TOOLS = ("Edit", "MultiEdit", "Write", "NotebookEdit", "Read")
SHELL_TOOLS = ("Bash", "PowerShell", "BashOutput")
UNSAFE_CHARS = ("&", ";", "|", "`", ">", "<", "$(", "\n")

# Этим командам кнопку «Разрешить всегда» не предлагаем: одно случайное
# нажатие разрешило бы навсегда любое их применение, включая разрушительное.
DANGEROUS_HEADS = (
    "rm", "rmdir", "del", "erase", "rd", "mv", "move", "format", "mkfs",
    "dd", "chmod", "chown", "icacls", "takeown", "shutdown", "reg",
    "curl", "wget", "iwr", "invoke-webrequest", "ssh", "scp", "sudo",
    "kill", "taskkill", "schtasks", "net", "sc", "diskpart",
)


# --------------------------------------------------------------------------- #
# Мелкие утилиты: ни одна из них не должна бросить исключение наружу
# --------------------------------------------------------------------------- #

def debug(msg):
    if DEBUG:
        sys.stderr.write("[tg_approve] %s\n" % msg)
        sys.stderr.flush()


def log(record):
    """Дописывает строку в журнал решений. Молча игнорирует любые проблемы."""
    try:
        record["ts"] = time.strftime("%Y-%m-%d %H:%M:%S")
        with open(LOG_PATH, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception as exc:
        debug("не удалось записать журнал: %s" % exc)


def read_env():
    """Настройки: сначала .env рядом со скриптом, потом переменные окружения."""
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
    for key in ("TG_TOKEN", "TG_CHAT_ID", "CC_TG_TIMEOUT", "CC_TG_DEBUG"):
        if os.environ.get(key):
            data[key] = os.environ[key].strip()
    return data


def clip(text, limit=MAX_FIELD):
    text = str(text)
    return text if len(text) <= limit else text[:limit] + " …"


# --------------------------------------------------------------------------- #
# Общение с Telegram
# --------------------------------------------------------------------------- #

class TgFail(Exception):
    def __init__(self, code=0, detail=""):
        super().__init__(detail or str(code))
        self.code = code


def api(token, method, payload, timeout=30):
    """Вызов метода Telegram. При любой неудаче поднимает TgFail."""
    url = "https://api.telegram.org/bot%s/%s" % (token, method)
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url, data=body, headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            answer = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise TgFail(exc.code, "%s -> HTTP %s" % (method, exc.code))
    except Exception as exc:
        raise TgFail(0, "%s -> %s" % (method, exc))
    if not answer.get("ok"):
        raise TgFail(0, "%s -> %s" % (method, answer.get("description")))
    return answer.get("result")


# --------------------------------------------------------------------------- #
# Текст сообщения
# --------------------------------------------------------------------------- #

def preview(text, lines=15):
    """Первые N строк текста для вставки в сообщение."""
    all_lines = str(text).splitlines()
    out = "\n".join(all_lines[:lines])
    if len(all_lines) > lines:
        out += "\n…"
    return clip(out, 900)


def describe(tool_name, tool_input):
    """Человекочитаемое описание запроса: список готовых строк в HTML."""
    esc = html.escape
    rows = []

    if tool_name in SHELL_TOOLS and tool_input.get("command"):
        rows.append("<pre>%s</pre>" % esc(clip(tool_input["command"])))
        if tool_input.get("description"):
            rows.append("💬 <i>%s</i>" % esc(clip(tool_input["description"], 200)))

    elif tool_name in ("Edit", "MultiEdit"):
        rows.append("📄 <code>%s</code>" % esc(str(tool_input.get("file_path", "?"))))
        edits = tool_input.get("edits") or [tool_input]
        for edit in edits[:3]:
            if not isinstance(edit, dict):
                continue
            if edit.get("old_string"):
                rows.append("было:\n<pre>%s</pre>" % esc(preview(edit["old_string"], 8)))
            if edit.get("new_string"):
                rows.append("станет:\n<pre>%s</pre>" % esc(preview(edit["new_string"], 8)))
        if len(edits) > 3:
            rows.append("<i>…и ещё %d правок</i>" % (len(edits) - 3))

    elif tool_name == "Write":
        rows.append("📄 <code>%s</code>" % esc(str(tool_input.get("file_path", "?"))))
        if tool_input.get("content"):
            rows.append("содержимое:\n<pre>%s</pre>"
                        % esc(preview(tool_input["content"], 15)))

    elif tool_name in ("Read", "NotebookEdit", "Glob", "Grep"):
        target = (
            tool_input.get("file_path")
            or tool_input.get("notebook_path")
            or tool_input.get("pattern")
            or tool_input.get("path")
            or "?"
        )
        rows.append("📄 <code>%s</code>" % esc(str(target)))

    elif tool_name in ("WebFetch", "WebSearch"):
        rows.append("🌐 <code>%s</code>"
                    % esc(str(tool_input.get("url") or tool_input.get("query") or "?")))

    if not rows:
        try:
            dump = json.dumps(tool_input, ensure_ascii=False)
        except Exception:
            dump = str(tool_input)
        rows.append("<pre>%s</pre>" % esc(clip(dump)))

    return rows


def build_text(payload, rule_label=None):
    esc = html.escape
    cwd = str(payload.get("cwd") or "")
    project = os.path.basename(cwd.rstrip("\\/")) or cwd or "проект"
    tool_name = str(payload.get("tool_name") or "?")
    tool_input = payload.get("tool_input") or {}
    if not isinstance(tool_input, dict):
        tool_input = {"value": tool_input}

    rows = ["🔔 <b>%s</b>" % esc(project), "🔧 <code>%s</code>" % esc(tool_name)]
    rows += describe(tool_name, tool_input)

    context = payload.get("permission_context")
    reason = context.get("reason") if isinstance(context, dict) else None
    if reason:
        rows.append("ℹ️ <i>%s</i>" % esc(clip(str(reason), 200)))
    if rule_label:
        rows.append("♾ «Всегда» добавит правило: <code>%s</code>" % esc(rule_label))
    rows.append("📁 <i>%s</i>" % esc(clip(cwd, 120)))

    return clip("\n".join(rows), MAX_TEXT)


# --------------------------------------------------------------------------- #
# Правило для кнопки «Разрешить всегда»
# --------------------------------------------------------------------------- #

def derive_rule(tool_name, tool_input, cwd):
    """Возвращает (инструмент, правило) или None, если правило не выводится."""
    try:
        if tool_name in SHELL_TOOLS:
            command = str(tool_input.get("command") or "").strip()
            if not command or any(ch in command for ch in UNSAFE_CHARS):
                return None
            words = command.split()
            if not words:
                return None
            head = words[0]
            if not re.match(r"^[A-Za-z][A-Za-z0-9._-]*$", head):
                return None
            if head.lower().rsplit(".", 1)[0] in DANGEROUS_HEADS:
                return None
            if len(words) > 1 and re.match(r"^[a-z][a-z0-9-]*$", words[1]):
                return tool_name, "%s %s *" % (head, words[1])
            return tool_name, "%s *" % head

        if tool_name in FILE_TOOLS and cwd:
            return tool_name, os.path.join(cwd, "**")
    except Exception as exc:
        debug("не удалось вывести правило: %s" % exc)
    return None


# --------------------------------------------------------------------------- #
# Безопасный список
# --------------------------------------------------------------------------- #

def safelisted(tool_name, tool_input):
    """True, если команду можно одобрить сразу, не беспокоя Telegram."""
    try:
        if tool_name not in SHELL_TOOLS:
            return False
        command = str(tool_input.get("command") or "").strip()
        if not command or any(ch in command for ch in UNSAFE_CHARS):
            return False
        norm = re.sub(r"\s+", " ", command).lower()
        path = HERE / "safelist.txt"
        if not path.exists():
            return False
        for line in path.read_text(encoding="utf-8").splitlines():
            pattern = re.sub(r"\s+", " ", line.strip()).lower()
            if not pattern or pattern.startswith("#"):
                continue
            if norm == pattern or norm.startswith(pattern + " "):
                return True
    except Exception as exc:
        debug("безопасный список не прочитан: %s" % exc)
    return False


# --------------------------------------------------------------------------- #
# Флажок («занято») и полочка для чужих нажатий
# --------------------------------------------------------------------------- #

def prepare_state_dir():
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        now = time.time()
        for item in STATE_DIR.glob("cb-*.json"):
            try:
                if now - item.stat().st_mtime > MAILBOX_TTL:
                    item.unlink()
            except OSError:
                pass
    except Exception as exc:
        debug("не удалось подготовить рабочую папку: %s" % exc)


def take_lock():
    try:
        fd = os.open(str(LOCK_PATH), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        try:
            os.write(fd, ("%d %f" % (os.getpid(), time.time())).encode("ascii"))
        finally:
            os.close(fd)
        return True
    except FileExistsError:
        try:
            if time.time() - LOCK_PATH.stat().st_mtime > LOCK_STALE:
                LOCK_PATH.unlink()
                debug("снял брошенный флажок")
        except OSError:
            pass
        return False
    except Exception as exc:
        debug("флажок не взят: %s" % exc)
        return False


def drop_lock():
    try:
        LOCK_PATH.unlink()
    except OSError:
        pass


def read_offset():
    try:
        return int(OFFSET_PATH.read_text(encoding="ascii").strip())
    except Exception:
        return None


def write_offset(value):
    try:
        OFFSET_PATH.write_text(str(int(value)), encoding="ascii")
    except Exception as exc:
        debug("не удалось сохранить номер письма: %s" % exc)


def mailbox_put(rid, press):
    if not re.match(r"^[0-9a-f]{4,16}$", str(rid or "")):
        return
    try:
        target = STATE_DIR / ("cb-%s.json" % rid)
        tmp = STATE_DIR / ("cb-%s.%d.tmp" % (rid, os.getpid()))
        tmp.write_text(json.dumps(press), encoding="utf-8")
        os.replace(str(tmp), str(target))
        debug("положил на полочку нажатие для %s" % rid)
    except Exception as exc:
        debug("не удалось положить на полочку: %s" % exc)


def mailbox_take(rid):
    target = STATE_DIR / ("cb-%s.json" % rid)
    try:
        if not target.exists():
            return None
        press = json.loads(target.read_text(encoding="utf-8"))
        target.unlink()
        debug("забрал своё нажатие с полочки")
        return press
    except Exception as exc:
        debug("не удалось забрать с полочки: %s" % exc)
        return None


# --------------------------------------------------------------------------- #
# Ожидание нажатия
# --------------------------------------------------------------------------- #

def parse_press(callback, chat_id, token):
    """Разбор нажатия. Возвращает (номер запроса, данные) или (None, None)."""
    sender = str((callback.get("from") or {}).get("id") or "")
    if sender != str(chat_id):
        debug("нажатие от посторонних (%s) — игнорирую" % sender)
        try:
            api(token, "answerCallbackQuery",
                {"callback_query_id": callback.get("id"),
                 "text": "Нет доступа", "show_alert": True}, timeout=10)
        except TgFail:
            pass
        return None, None
    action, _, rid = str(callback.get("data") or "").partition(":")
    if action not in ("a", "d", "r") or not rid:
        return None, None
    message = callback.get("message") or {}
    return rid, {
        "action": action,
        "callback_id": callback.get("id"),
        "message_id": message.get("message_id"),
        "chat_id": (message.get("chat") or {}).get("id"),
    }


def wait_for_press(token, chat_id, rid, deadline):
    """Круг «полочка → флажок → вопрос Telegram», пока не истечёт время."""
    while time.time() < deadline:
        press = mailbox_take(rid)
        if press:
            return press

        if not take_lock():
            time.sleep(1)
            continue

        try:
            left = deadline - time.time()
            if left <= 0:
                return None
            offset = read_offset()
            if offset is None:
                # Первый запуск: берём только самое свежее письмо, не всю историю.
                params = {"offset": -1, "timeout": 0}
            else:
                params = {"offset": offset,
                          "timeout": max(1, min(LONG_POLL, int(left)))}
            params["allowed_updates"] = ["callback_query"]
            try:
                updates = api(token, "getUpdates", params,
                              timeout=params["timeout"] + 20) or []
            except TgFail as exc:
                debug("getUpdates не удался: %s" % exc)
                time.sleep(2)
                continue

            mine = None
            for update in updates:
                if "update_id" in update:
                    write_offset(update["update_id"] + 1)
                callback = update.get("callback_query")
                if not callback:
                    continue
                got, press = parse_press(callback, chat_id, token)
                if not got:
                    continue
                if got == rid:
                    mine = press
                else:
                    mailbox_put(got, press)
            if mine:
                return mine
        finally:
            drop_lock()
    return None


# --------------------------------------------------------------------------- #
# Решение для Claude Code
# --------------------------------------------------------------------------- #

def emit(behavior, message=None, rule=None):
    decision = {"behavior": behavior}
    if message:
        decision["message"] = message
    if rule:
        decision["addPermissionRule"] = {
            "tool": rule[0], "rule": rule[1], "mode": "allow"
        }
    out = {"hookSpecificOutput": {"hookEventName": HOOK_EVENT, "decision": decision}}
    # ensure_ascii по умолчанию: вывод чистый ASCII, безопасен в любой консоли.
    sys.stdout.write(json.dumps(out))
    sys.stdout.flush()


def edit_message(token, chat_id, message_id, text, footer):
    """Переписывает сообщение с итогом. reply_markup не передаём — кнопки уходят."""
    if not message_id:
        return
    try:
        api(token, "editMessageText", {
            "chat_id": chat_id,
            "message_id": message_id,
            "text": clip(text + "\n\n" + footer, MAX_TEXT + 200),
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }, timeout=15)
    except TgFail as exc:
        debug("не удалось переписать сообщение: %s" % exc)


# --------------------------------------------------------------------------- #

def main():
    global DEBUG, WAIT_TIMEOUT

    raw = sys.stdin.read()
    if not raw or not raw.strip():
        debug("пустой вход")
        return
    payload = json.loads(raw)
    if not isinstance(payload, dict):
        debug("вход не является объектом")
        return

    env = read_env()
    DEBUG = env.get("CC_TG_DEBUG", "0") not in ("0", "", "false", "no")
    try:
        WAIT_TIMEOUT = max(5, int(env.get("CC_TG_TIMEOUT") or WAIT_TIMEOUT))
    except ValueError:
        pass

    token = env.get("TG_TOKEN", "")
    chat_id = env.get("TG_CHAT_ID", "")
    if not token or not chat_id:
        debug("не заполнены TG_TOKEN или TG_CHAT_ID — выхожу молча")
        return

    # Страховочный таймер: что бы ни случилось, скрипт завершится сам.
    threading.Thread(
        target=lambda: (time.sleep(WAIT_TIMEOUT + WATCHDOG_EXTRA), os._exit(0)),
        daemon=True,
    ).start()

    tool_name = str(payload.get("tool_name") or "?")
    tool_input = payload.get("tool_input") or {}
    if not isinstance(tool_input, dict):
        tool_input = {"value": tool_input}
    cwd = str(payload.get("cwd") or "")
    base = {
        "tool": tool_name,
        "cwd": cwd,
        "session": payload.get("session_id"),
        "tool_use_id": payload.get("tool_use_id"),
    }

    if safelisted(tool_name, tool_input):
        log(dict(base, decision="allow", via="safelist",
                 command=clip(tool_input.get("command", ""), 200)))
        emit("allow")
        return

    rid = uuid.uuid4().hex[:8]
    rule = derive_rule(tool_name, tool_input, cwd)
    rule_label = rule[1] if rule else None

    keyboard = [[
        {"text": "✅ Разрешить", "callback_data": "a:%s" % rid},
        {"text": "❌ Запретить", "callback_data": "d:%s" % rid},
    ]]
    if rule:
        # Для команд на кнопке видно само правило, для файлов путь слишком
        # длинный — пишем словами, а полное правило остаётся в тексте.
        label = ("♾ Всегда: %s" % rule[1] if tool_name in SHELL_TOOLS
                 else "♾ Всегда: %s в этой папке" % tool_name)
        keyboard.append([{"text": clip(label, 48), "callback_data": "r:%s" % rid}])

    text = build_text(payload, rule_label)
    prepare_state_dir()

    try:
        sent = api(token, "sendMessage", {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
            "reply_markup": {"inline_keyboard": keyboard},
        }, timeout=20)
    except TgFail as exc:
        debug("сообщение не отправлено: %s" % exc)
        log(dict(base, decision="none", via="send_failed", error=str(exc)))
        return

    message_id = (sent or {}).get("message_id")
    chat_for_edit = ((sent or {}).get("chat") or {}).get("id", chat_id)
    debug("отправлено сообщение %s, запрос %s" % (message_id, rid))

    press = wait_for_press(token, chat_id, rid, time.time() + WAIT_TIMEOUT)
    stamp = time.strftime("%H:%M")

    if not press:
        edit_message(token, chat_for_edit, message_id, text,
                     "⏳ Время истекло в %s — вопрос ушёл в терминал" % stamp)
        log(dict(base, decision="none", via="timeout", rid=rid))
        return

    action = press.get("action")
    if action == "a":
        footer, toast = "✅ Разрешено в %s" % stamp, "Разрешено"
    elif action == "r":
        footer = "♾ Разрешено навсегда в %s · правило %s" % (stamp, rule_label)
        toast = "Разрешено навсегда"
    else:
        footer, toast = "❌ Запрещено в %s" % stamp, "Запрещено"

    try:
        api(token, "answerCallbackQuery",
            {"callback_query_id": press.get("callback_id"), "text": toast}, timeout=10)
    except TgFail as exc:
        debug("не удалось подтвердить нажатие: %s" % exc)

    edit_message(token, press.get("chat_id") or chat_for_edit,
                 press.get("message_id") or message_id, text, footer)

    if action == "a":
        log(dict(base, decision="allow", via="telegram", rid=rid))
        emit("allow")
    elif action == "r":
        log(dict(base, decision="allow", via="telegram_always",
                 rid=rid, rule=rule_label))
        emit("allow", rule=rule)
    else:
        log(dict(base, decision="deny", via="telegram", rid=rid))
        emit("deny", message="Запрещено из Telegram")


if __name__ == "__main__":
    try:
        main()
    except BaseException as exc:          # молчать обязаны при любой ошибке
        debug("ошибка: %r" % exc)
        if DEBUG:
            import traceback
            traceback.print_exc()
        try:
            log({"decision": "none", "via": "error", "error": repr(exc)})
        except Exception:
            pass
    sys.exit(0)
