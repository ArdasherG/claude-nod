#!/usr/bin/env python3
"""Хук Claude Code на событие PermissionRequest: спрашивает разрешение в Telegram.

Как работает:
  Claude Code запускает этот скрипт и передаёт ему на вход (stdin) описание
  запроса. Скрипт отправляет сообщение в Telegram с кнопками и ждёт нажатия.
  «Разрешить» — печатает решение allow, «Запретить» — deny.
  Ничего не напечатал — Claude Code задаст обычный вопрос в терминале.

Главный принцип: любая ошибка и любой таймаут = пустой вывод и код выхода 0.
Claude Code не должен зависнуть или сломаться ни при каком раскладе.
"""
import json
import os
import re
import sys
import threading
import time
import uuid

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import tg_common as tg                                            # noqa: E402

HOOK_EVENT = "PermissionRequest"
WAIT_DEFAULT = 300      # сколько всего ждём нажатия, секунд
WATCHDOG_EXTRA = 20     # страховочный таймер = ожидание + столько
REASON_WAIT = 120       # сколько ждём текст причины после кнопки «с причиной»
MAX_FIELD = 500         # обрезка команды и параметров в сообщении

_TOKEN = ""             # заполняются в main(), чтобы pic() был коротким
_CHAT = ""
_SMODE = "off"
_STICKERS = {}

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
# Разбор запроса
# --------------------------------------------------------------------------- #

def pic(name, quiet=False, mode=None):
    """Отправляет стикер события. Молча ничего не делает, если он не задан."""
    tg.sticker(_TOKEN, _CHAT, name, mode or _SMODE, quiet=quiet,
               stickers=_STICKERS)


def sticker_for_request(tool_name, tool_input):
    """Какой стикер показать перед вопросом."""
    if tool_name in SHELL_TOOLS:
        command = str(tool_input.get("command") or "").strip()
        head = (command.split() or [""])[0].lower().rsplit(".", 1)[0]
        # curl/wget тоже попадают сюда: они в списке опасных, и это сознательно —
        # «осторожно» важнее, чем «это про сеть».
        if head in DANGEROUS_HEADS:
            return "ask_danger"
        return "ask_bash"
    if tool_name in ("Edit", "MultiEdit", "NotebookEdit"):
        return "ask_edit"
    if tool_name == "Write":
        return "ask_write"
    if tool_name in ("WebFetch", "WebSearch"):
        return "ask_net"
    return "ask_other"


def tool_detail(tool_name, tool_input):
    """Главное, что нужно знать о запросе: команда, путь или параметры."""
    if tool_name in SHELL_TOOLS:
        return str(tool_input.get("command") or "")
    for field in ("file_path", "notebook_path", "url", "pattern", "path", "query"):
        if tool_input.get(field):
            return str(tool_input[field])
    try:
        return json.dumps(tool_input, ensure_ascii=False, sort_keys=True)
    except Exception:
        return str(tool_input)


def session_key(tool_name, tool_input):
    """Ключ для кэша «разрешить до конца сессии»."""
    detail = re.sub(r"\s+", " ", tool_detail(tool_name, tool_input)).strip()
    return "%s|%s" % (tool_name, detail.lower())


def describe(tool_name, tool_input):
    """Человекочитаемое описание запроса: список готовых строк в HTML."""
    rows = []

    if tool_name in SHELL_TOOLS and tool_input.get("command"):
        rows.append("<pre>%s</pre>" % tg.esc(tg.clip(tool_input["command"], MAX_FIELD)))
        if tool_input.get("description"):
            rows.append("💬 <i>%s</i>"
                        % tg.esc(tg.clip(tool_input["description"], 200)))

    elif tool_name in ("Edit", "MultiEdit"):
        rows.append("📄 <code>%s</code>"
                    % tg.esc(str(tool_input.get("file_path", "?"))))
        edits = tool_input.get("edits") or [tool_input]
        for edit in edits[:3]:
            if not isinstance(edit, dict):
                continue
            if edit.get("old_string"):
                rows.append("было:\n<pre>%s</pre>"
                            % tg.esc(tg.clip(tg.head_lines(edit["old_string"], 8), 900)))
            if edit.get("new_string"):
                rows.append("станет:\n<pre>%s</pre>"
                            % tg.esc(tg.clip(tg.head_lines(edit["new_string"], 8), 900)))
        if len(edits) > 3:
            rows.append("<i>…и ещё %d правок</i>" % (len(edits) - 3))

    elif tool_name == "Write":
        rows.append("📄 <code>%s</code>"
                    % tg.esc(str(tool_input.get("file_path", "?"))))
        if tool_input.get("content"):
            rows.append("содержимое:\n<pre>%s</pre>"
                        % tg.esc(tg.clip(tg.head_lines(tool_input["content"], 15), 900)))

    elif tool_name in ("Read", "NotebookEdit", "Glob", "Grep"):
        rows.append("📄 <code>%s</code>"
                    % tg.esc(tool_detail(tool_name, tool_input)))

    elif tool_name in ("WebFetch", "WebSearch"):
        rows.append("🌐 <code>%s</code>"
                    % tg.esc(tool_detail(tool_name, tool_input)))

    if not rows:
        rows.append("<pre>%s</pre>"
                    % tg.esc(tg.clip(tool_detail(tool_name, tool_input), MAX_FIELD)))
    return rows


def build_text(payload, rid, rule_label=None):
    """Текст сообщения. Номер запроса показываем, чтобы отличать похожие."""
    cwd = str(payload.get("cwd") or "")
    project = os.path.basename(cwd.rstrip("\\/")) or cwd or "проект"
    tool_name = str(payload.get("tool_name") or "?")
    tool_input = payload.get("tool_input") or {}
    if not isinstance(tool_input, dict):
        tool_input = {"value": tool_input}
    session = str(payload.get("session_id") or "")[:4]

    head = "🔔 <b>%s</b> · <code>#%s</code>" % (tg.esc(project), tg.esc(rid[:4]))
    if session:
        head += " · сессия <code>%s</code>" % tg.esc(session)
    rows = [head, "🔧 <code>%s</code>" % tg.esc(tool_name)]
    rows += describe(tool_name, tool_input)

    context = payload.get("permission_context")
    reason = context.get("reason") if isinstance(context, dict) else None
    if reason:
        rows.append("ℹ️ <i>%s</i>" % tg.esc(tg.clip(str(reason), 200)))
    if rule_label:
        rows.append("♾ «Всегда» добавит правило: <code>%s</code>"
                    % tg.esc(rule_label))
    rows.append("📁 <i>%s</i>" % tg.esc(tg.clip(cwd, 120)))
    return tg.clip("\n".join(rows), tg.CHUNK)


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
        tg.debug("правило не выведено: %s" % exc)
    return None


# --------------------------------------------------------------------------- #
# Безопасный список
# --------------------------------------------------------------------------- #

def safelisted(tool_name, tool_input, path=None):
    """True, если команду можно одобрить сразу, не беспокоя Telegram."""
    try:
        if tool_name not in SHELL_TOOLS:
            return False
        command = str(tool_input.get("command") or "").strip()
        if not command or any(ch in command for ch in UNSAFE_CHARS):
            return False
        norm = re.sub(r"\s+", " ", command).lower()
        path = path or (tg.HERE / "safelist.txt")
        if not path.exists():
            return False
        for line in path.read_text(encoding="utf-8").splitlines():
            pattern = re.sub(r"\s+", " ", line.strip()).lower()
            if not pattern or pattern.startswith("#"):
                continue
            if norm == pattern or norm.startswith(pattern + " "):
                return True
    except Exception as exc:
        tg.debug("безопасный список не прочитан: %s" % exc)
    return False


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


def keyboard_for(rid, rule, with_session):
    rows = [[
        {"text": "✅ Разрешить", "callback_data": "a:%s" % rid},
        {"text": "❌ Запретить", "callback_data": "d:%s" % rid},
    ]]
    second = []
    if with_session:
        second.append({"text": "⏳ До конца сессии", "callback_data": "s:%s" % rid})
    second.append({"text": "✏️ Запретить с причиной", "callback_data": "x:%s" % rid})
    rows.append(second)
    if rule:
        # Для команд на кнопке видно само правило, для файлов путь слишком
        # длинный — пишем словами, а полное правило остаётся в тексте.
        label = ("♾ Всегда: %s" % rule[1] if rule[0] in SHELL_TOOLS
                 else "♾ Всегда: %s в этой папке" % rule[0])
        rows.append([{"text": tg.clip(label, 48), "callback_data": "r:%s" % rid}])
    return rows


def confirm_keyboard(rid, rule_label):
    return [[
        {"text": tg.clip("♾ Да, навсегда: %s" % rule_label, 48),
         "callback_data": "R:%s" % rid},
    ], [
        {"text": "⬅ Отмена", "callback_data": "c:%s" % rid},
    ]]


# --------------------------------------------------------------------------- #

def main():
    raw = sys.stdin.read()
    if not raw or not raw.strip():
        tg.debug("пустой вход")
        return
    payload = json.loads(raw)
    if not isinstance(payload, dict):
        tg.debug("вход не является объектом")
        return

    env = tg.read_env()
    token = env.get("TG_TOKEN", "")
    chat_id = env.get("TG_CHAT_ID", "")
    if not token or not chat_id:
        tg.debug("не заполнены TG_TOKEN или TG_CHAT_ID — выхожу молча")
        return

    global _TOKEN, _CHAT, _SMODE, _STICKERS
    _TOKEN, _CHAT = token, chat_id
    _STICKERS = tg.load_stickers()
    _SMODE = tg.sticker_mode(env, _STICKERS)

    wait_timeout = tg.number(env, "CC_TG_TIMEOUT", WAIT_DEFAULT, low=5, high=3000)
    # Не ждём дольше, чем Claude Code готов терпеть хук: иначе нас оборвут,
    # и нажатие, сделанное в последний момент, пропадёт.
    limit = tg.hook_timeout(HOOK_EVENT)
    if limit and wait_timeout > limit - WATCHDOG_EXTRA - 5:
        wait_timeout = max(5, int(limit - WATCHDOG_EXTRA - 5))
        tg.debug("ожидание ужато до %d с под лимит хука %d с" % (wait_timeout, limit))
    threading.Thread(                     # страховка: скрипт завершится сам
        target=lambda: (time.sleep(wait_timeout + WATCHDOG_EXTRA), os._exit(0)),
        daemon=True,
    ).start()

    tool_name = str(payload.get("tool_name") or "?")
    tool_input = payload.get("tool_input") or {}
    if not isinstance(tool_input, dict):
        tool_input = {"value": tool_input}
    cwd = str(payload.get("cwd") or "")
    session_id = payload.get("session_id")
    base = {
        "tool": tool_name,
        "cwd": cwd,
        "session": session_id,
        "tool_use_id": payload.get("tool_use_id"),
    }
    detail = tg.clip(tool_detail(tool_name, tool_input), 200)

    # Тихие часы: ночью не будим, вопрос уходит в терминал.
    if tg.quiet_now(env.get("CC_TG_QUIET")):
        tg.log(dict(base, decision="none", via="quiet_hours", detail=detail))
        return

    # Безопасный список: решаем на месте, в Telegram ничего не отправляем.
    if safelisted(tool_name, tool_input):
        tg.log(dict(base, decision="allow", via="safelist", detail=detail))
        # Смысл безопасного списка — не беспокоить, поэтому стикер только
        # в режиме «all», и всегда без звука.
        pic("safelist", quiet=True, mode=("all" if _SMODE == "all" else "off"))
        emit("allow")
        return

    tg.prepare_state_dir()

    # Уже разрешено до конца этой сессии.
    key = session_key(tool_name, tool_input)
    if session_id and tg.session_allowed(session_id, key):
        tg.log(dict(base, decision="allow", via="session_cache", detail=detail))
        emit("allow")
        return

    rid = uuid.uuid4().hex[:8]
    rule = derive_rule(tool_name, tool_input, cwd)
    rule_label = rule[1] if rule else None
    text = build_text(payload, rid, rule_label)
    keyboard = keyboard_for(rid, rule, bool(session_id))

    pic(sticker_for_request(tool_name, tool_input), quiet=True)
    try:
        sent = tg.send(token, chat_id, text, keyboard=keyboard)
    except tg.TgFail as exc:
        tg.debug("сообщение не отправлено: %s" % exc)
        tg.log(dict(base, decision="none", via="send_failed", error=str(exc)))
        return

    message_id = (sent or {}).get("message_id")
    chat_for_edit = ((sent or {}).get("chat") or {}).get("id", chat_id)
    tg.debug("отправлено сообщение %s, запрос %s" % (message_id, rid))

    deadline = time.time() + wait_timeout
    decided = handle(token, chat_id, chat_for_edit, message_id, rid, text,
                     rule, rule_label, session_id, key, deadline, base, detail)
    if not decided:
        tg.edit(token, chat_for_edit, message_id,
                text + "\n\n⏳ Время истекло в %s — вопрос ушёл в терминал"
                % time.strftime("%H:%M"))
        pic("timeout")
        tg.log(dict(base, decision="none", via="timeout", rid=rid, detail=detail))


def handle(token, chat_id, chat_for_edit, message_id, rid, text, rule,
           rule_label, session_id, key, deadline, base, detail):
    """Ждёт и обрабатывает нажатия, пока не будет окончательного решения.

    Возвращает True, если решение напечатано, и False, если истекло время.
    «Всегда» требует подтверждения, поэтому цикл: подтвердили — решаем,
    отменили — возвращаем исходные кнопки и ждём снова.
    """
    while time.time() < deadline:
        kind, data = tg.wait(token, chat_id, deadline, rid=rid)
        if kind != "press":
            return False

        action = data.get("action")
        callback_id = data.get("callback_id")
        chat = data.get("chat_id") or chat_for_edit
        msg = data.get("message_id") or message_id
        stamp = time.strftime("%H:%M")

        if action == "a":
            tg.answer_press(token, callback_id, "Разрешено")
            pic("allowed")
            tg.edit(token, chat, msg, text + "\n\n✅ Разрешено в %s" % stamp)
            tg.log(dict(base, decision="allow", via="telegram", rid=rid,
                        detail=detail))
            emit("allow")
            return True

        if action == "s":
            tg.answer_press(token, callback_id, "Разрешено до конца сессии")
            pic("session_allowed")
            if session_id:
                tg.session_allow(session_id, key)
            tg.edit(token, chat, msg,
                    text + "\n\n⏳ Разрешено до конца сессии в %s" % stamp)
            tg.log(dict(base, decision="allow", via="telegram_session", rid=rid,
                        detail=detail))
            emit("allow")
            return True

        if action == "d":
            tg.answer_press(token, callback_id, "Запрещено")
            pic("denied")
            tg.edit(token, chat, msg, text + "\n\n❌ Запрещено в %s" % stamp)
            tg.log(dict(base, decision="deny", via="telegram", rid=rid,
                        detail=detail))
            emit("deny", message="Запрещено из Telegram")
            return True

        if action == "x":
            tg.answer_press(token, callback_id, "Жду причину")
            pic("waiting_reply", quiet=True)
            return deny_with_reason(token, chat_id, chat, msg, rid, text,
                                   deadline, base, detail)

        if action == "r" and rule:
            # Второй шаг: правило действует постоянно, спрашиваем ещё раз.
            tg.answer_press(token, callback_id, "Подтвердите правило")
            tg.edit(token, chat, msg,
                    text + "\n\n♾ <b>Подтвердите:</b> правило <code>%s</code> "
                           "останется навсегда" % tg.esc(rule_label))
            try:
                tg.edit_keyboard(token, chat, msg, confirm_keyboard(rid, rule_label))
            except tg.TgFail as exc:
                tg.debug("кнопки подтверждения не поставлены: %s" % exc)
            continue

        if action == "R" and rule:
            tg.answer_press(token, callback_id, "Разрешено навсегда")
            pic("always_allowed")
            tg.edit(token, chat, msg,
                    text + "\n\n♾ Разрешено навсегда в %s · правило <code>%s</code>"
                    % (stamp, tg.esc(rule_label)))
            tg.log(dict(base, decision="allow", via="telegram_always", rid=rid,
                        rule=rule_label, detail=detail))
            emit("allow", rule=rule)
            return True

        if action == "c":
            tg.answer_press(token, callback_id, "Отменено")
            tg.edit(token, chat, msg, text)
            try:
                tg.edit_keyboard(token, chat, msg,
                                 keyboard_for(rid, rule, bool(session_id)))
            except tg.TgFail as exc:
                tg.debug("кнопки не возвращены: %s" % exc)
            continue

        tg.debug("неизвестное действие %r" % action)
    return False


def deny_with_reason(token, chat_id, chat, msg, rid, text, deadline, base, detail):
    """Просит написать причину отказа; её текст уходит обратно в Claude."""
    ask = text + ("\n\n✏️ <b>Напишите причину отказа</b> ответом на это сообщение."
                  "\nБез ответа запрет уйдёт без пояснения.")
    tg.edit(token, chat, msg, ask)
    limit = min(deadline, time.time() + REASON_WAIT)
    kind, data = tg.wait(token, chat_id, limit, rid=rid, want_text=True,
                         reply_to=msg)
    stamp = time.strftime("%H:%M")

    reason = ""
    if kind == "text":
        reason = tg.clip(data.get("text", "").strip(), 400)
    elif kind == "press" and data.get("action") == "a":
        # Передумали и нажали «Разрешить» — уважаем последнее действие.
        tg.answer_press(token, data.get("callback_id"), "Разрешено")
        pic("allowed")
        tg.edit(token, chat, msg, text + "\n\n✅ Разрешено в %s" % stamp)
        tg.log(dict(base, decision="allow", via="telegram", rid=rid, detail=detail))
        emit("allow")
        return True

    footer = "❌ Запрещено в %s" % stamp
    if reason:
        footer += "\n💬 <i>%s</i>" % tg.esc(reason)
    tg.edit(token, chat, msg, text + "\n\n" + footer)
    pic("denied_reason" if reason else "denied")
    tg.log(dict(base, decision="deny", via="telegram_reason", rid=rid,
                reason=reason, detail=detail))
    emit("deny", message=reason or "Запрещено из Telegram")
    return True


if __name__ == "__main__":
    try:
        main()
    except BaseException as exc:          # молчать обязаны при любой ошибке
        tg.debug("ошибка: %r" % exc)
        try:
            tg.log({"decision": "none", "via": "error", "error": repr(exc)})
        except Exception:
            pass
    sys.exit(0)
