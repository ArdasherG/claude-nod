#!/usr/bin/env python3
"""Хук Claude Code на событие PermissionRequest: спрашивает разрешение в Telegram.

Как работает:
  Claude Code запускает этот скрипт и передаёт ему на вход (stdin) описание
  запроса. Скрипт отправляет сообщение в Telegram с кнопками и ждёт нажатия.
  «Разрешить» — печатает решение allow, «Запретить» — deny.
  Ничего не напечатал — Claude Code задаст обычный вопрос в терминале.

Главный принцип: любая ошибка и любой таймаут = пустой вывод и код выхода 0.
Claude Code не должен зависнуть или сломаться ни при каком раскладе.
А раз решение принято — оно печатается первым, до всякой косметики.
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

# Символы, с которыми команда перестаёт быть «одной простой командой»:
# цепочки, перенаправления, подстановки и блоки PowerShell.
UNSAFE_CHARS = ("&", ";", "|", "`", ">", "<", "$", "(", ")", "{", "}",
                "%", "^", "\n", "\r")

# Разрушительные и выходящие наружу команды: стикер «осторожно» и никакой
# кнопки «Всегда».
DANGEROUS_HEADS = (
    "rm", "rmdir", "del", "erase", "rd", "mv", "move", "format", "mkfs",
    "dd", "chmod", "chown", "icacls", "takeown", "shutdown", "reg",
    "curl", "wget", "iwr", "invoke-webrequest", "invoke-restmethod", "irm",
    "ssh", "scp", "sudo", "kill", "taskkill", "stop-process", "schtasks",
    "net", "sc", "diskpart", "remove-item", "ri", "move-item", "mi",
    "set-content", "clear-content", "out-file",
)

# Интерпретаторы: правило «python *» разрешило бы навсегда любой код.
INTERPRETERS = (
    "bash", "sh", "zsh", "fish", "cmd", "powershell", "pwsh", "python", "py",
    "node", "deno", "bun", "perl", "ruby", "php", "lua", "java", "dotnet",
    "npx", "pnpx", "bunx", "eval", "exec", "source", "invoke-expression",
    "iex", "start-process", "saps", "invoke-command", "icm", "docker",
    "kubectl", "wsl",
)

# Подкоманды, которые нельзя разрешать навсегда даже у безобидной программы.
RISKY_SUBCOMMANDS = {
    "git": ("clean", "reset", "rm", "restore", "filter-branch", "update-ref"),
    "npm": ("exec", "x"), "pnpm": ("exec", "dlx"), "yarn": ("exec", "dlx"),
}

# Флаги, при которых даже команда из безопасного списка что-то меняет или
# читает за пределами репозитория.
DENY_FLAG = re.compile(
    r"^(-[dmcfo]|--(delete|move|copy|force|output|no-index|ext-diff|exec"
    r"|upload-pack|receive-pack|config|global|system|file)\b.*)$", re.I)


# --------------------------------------------------------------------------- #
# Разбор запроса
# --------------------------------------------------------------------------- #

def pic(name, quiet=False, mode=None):
    """Отправляет стикер события. Молча ничего не делает, если он не задан."""
    tg.sticker(_TOKEN, _CHAT, name, mode or _SMODE, quiet=quiet,
               stickers=_STICKERS)


def program(word):
    """Имя программы без пути, расширения и номера версии: python3.12 -> python."""
    name = re.split(r"[\\/]", str(word or "").lower())[-1]
    name = re.sub(r"\.(exe|cmd|bat|ps1|com)$", "", name)
    return re.sub(r"[\d.]+$", "", name) or name


def sticker_for_request(tool_name, tool_input):
    """Какой стикер показать перед вопросом."""
    if tool_name in SHELL_TOOLS:
        command = str(tool_input.get("command") or "").strip()
        if program((command.split() or [""])[0]) in DANGEROUS_HEADS:
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


def preview(text, lines):
    return tg.esc(tg.clip(tg.redact(tg.head_lines(text, lines)), 900))


def describe(tool_name, tool_input):
    """Человекочитаемое описание запроса: список готовых строк в HTML.

    Первая строка — главное (команда или путь), дальше превью. При нехватке
    места превью отбрасываются с конца, а главное остаётся.
    """
    rows = []
    hidden = "<i>содержимое скрыто: файл похож на секреты</i>"

    if tool_name in SHELL_TOOLS and tool_input.get("command"):
        rows.append("<pre>%s</pre>"
                    % tg.esc(tg.clip(tg.redact(tool_input["command"]), MAX_FIELD)))
        if tool_input.get("description"):
            rows.append("💬 <i>%s</i>"
                        % tg.esc(tg.clip(tg.redact(tool_input["description"]), 200)))

    elif tool_name in ("Edit", "MultiEdit"):
        path = str(tool_input.get("file_path", "?"))
        rows.append("📄 <code>%s</code>" % tg.esc(path))
        edits = tool_input.get("edits") or [tool_input]
        if tg.sensitive_path(path):
            rows.append(hidden)
        else:
            for edit in edits[:3]:
                if not isinstance(edit, dict):
                    continue
                if edit.get("old_string"):
                    rows.append("было:\n<pre>%s</pre>" % preview(edit["old_string"], 8))
                if edit.get("new_string"):
                    rows.append("станет:\n<pre>%s</pre>" % preview(edit["new_string"], 8))
            if len(edits) > 3:
                rows.append("<i>…и ещё %d правок</i>" % (len(edits) - 3))

    elif tool_name == "Write":
        path = str(tool_input.get("file_path", "?"))
        rows.append("📄 <code>%s</code>" % tg.esc(path))
        if tg.sensitive_path(path):
            rows.append(hidden)
        elif tool_input.get("content"):
            rows.append("содержимое:\n<pre>%s</pre>"
                        % preview(tool_input["content"], 15))

    elif tool_name in ("Read", "NotebookEdit", "Glob", "Grep"):
        rows.append("📄 <code>%s</code>"
                    % tg.esc(tg.redact(tool_detail(tool_name, tool_input))))

    elif tool_name in ("WebFetch", "WebSearch"):
        rows.append("🌐 <code>%s</code>"
                    % tg.esc(tg.redact(tool_detail(tool_name, tool_input))))

    if not rows:
        rows.append("<pre>%s</pre>" % tg.esc(tg.clip(
            tg.redact(tool_detail(tool_name, tool_input)), MAX_FIELD)))
    return rows


def build_text(payload, rid, always_label=None):
    """Текст сообщения. Всегда валидный HTML и не длиннее CHUNK.

    Слепо обрезать готовый HTML нельзя: разрез внутри <pre> делает сообщение
    невалидным, Telegram его отвергает, и вопрос молча уходит в терминал.
    Поэтому при нехватке места выкидываем превью целыми строками.
    """
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
    top = [head, "🔧 <code>%s</code>" % tg.esc(tool_name)]

    tail = []
    context = payload.get("permission_context")
    reason = context.get("reason") if isinstance(context, dict) else None
    if reason:
        tail.append("ℹ️ <i>%s</i>" % tg.esc(tg.clip(tg.redact(str(reason)), 200)))
    if always_label:
        tail.append("♾ «Всегда» запишет правило <code>%s</code> в настройки "
                    "этого проекта" % tg.esc(always_label))
    tail.append("📁 <i>%s</i>" % tg.esc(tg.clip(cwd, 120)))

    body = describe(tool_name, tool_input)
    trimmed = False
    while len("\n".join(top + body + tail)) > tg.CHUNK and len(body) > 1:
        body = body[:-1]
        trimmed = True
    if trimmed:
        body.append("<i>…превью сокращено, целиком — в терминале</i>")
    text = "\n".join(top + body + tail)
    if len(text) > tg.CHUNK:                        # крайний случай: без разметки
        text = tg.esc(tg.clip(tg.plain(text), tg.CHUNK - 20))
    return text


# --------------------------------------------------------------------------- #
# Правила: «Всегда» и «До конца сессии»
# --------------------------------------------------------------------------- #

def vetted_words(command):
    """Слова команды, если её можно разрешать правилом, иначе None."""
    command = str(command or "").strip()
    if not command or "*" in command or any(ch in command for ch in UNSAFE_CHARS):
        return None
    words = command.split()
    if not re.match(r"^[A-Za-z][A-Za-z0-9._-]*$", words[0]):
        return None
    name = program(words[0])
    if name in DANGEROUS_HEADS or name in INTERPRETERS:
        return None
    if len(words) > 1 and words[1].lower() in RISKY_SUBCOMMANDS.get(name, ()):
        return None
    return words


def rule_ok(rule):
    """Можно ли записать это правило навсегда."""
    if not isinstance(rule, dict):
        return False
    tool = str(rule.get("toolName") or "")
    content = rule.get("ruleContent")
    if not tool:
        return False
    if tool in SHELL_TOOLS:
        if not isinstance(content, str) or not content.strip():
            return False                            # весь Bash целиком — нет
        text = content.strip()
        wildcard = bool(re.search(r"(:\*| \*)$", text))
        text = re.sub(r"(:\*| \*)$", "", text).strip()
        words = vetted_words(text)
        if not words:
            return False
        # «git *» накрыло бы и git clean, и git reset.
        if wildcard and len(words) == 1 and program(words[0]) in RISKY_SUBCOMMANDS:
            return False
        return True
    if tool in FILE_TOOLS:
        return isinstance(content, str) and bool(content.strip())
    return True


def label_of(rule):
    tool, content = rule.get("toolName"), rule.get("ruleContent")
    if tool in SHELL_TOOLS and content:
        return content
    return "%s(%s)" % (tool, content) if content else str(tool)


def derive_rule(tool_name, tool_input):
    """Правило для команды по её первым словам, или None."""
    if tool_name not in SHELL_TOOLS:
        return None
    words = vetted_words(tool_input.get("command"))
    if not words:
        return None
    if len(words) > 1 and re.match(r"^[a-z][a-z0-9-]*$", words[1]):
        content = "%s %s *" % (words[0], words[1])
    else:
        content = "%s *" % words[0]
    if not rule_ok({"toolName": tool_name, "ruleContent": content}):
        return None
    return tool_name, content


def suggestions(payload):
    """Подсказки правил от самого Claude Code, прошедшие нашу проверку."""
    for entry in payload.get("permission_suggestions") or []:
        if not isinstance(entry, dict):
            continue
        if entry.get("type") != "addRules" or entry.get("behavior") != "allow":
            continue
        rules = entry.get("rules") or []
        if rules and all(rule_ok(r) for r in rules):
            yield entry


def always_update(payload, tool_name, tool_input):
    """(подпись, запись updatedPermissions) для кнопки «Всегда», или None.

    Правило пишется в .claude/settings.local.json проекта, а не глобально.
    Сначала берём подсказку Claude Code — он лучше знает синтаксис правил,
    затем выводим сами по первым словам команды.
    """
    for entry in suggestions(payload):
        update = dict(entry, destination="localSettings")
        return label_of(entry["rules"][0]), update
    derived = derive_rule(tool_name, tool_input)
    if derived:
        rule = {"toolName": derived[0], "ruleContent": derived[1]}
        return label_of(rule), {"type": "addRules", "rules": [rule],
                                "behavior": "allow", "destination": "localSettings"}
    return None


def session_update(payload, tool_name, tool_input):
    """Запись updatedPermissions для «До конца сессии», или None.

    Правило живёт в памяти Claude Code и исчезает вместе с сессией. Для
    команды — ровно эта команда, без звёздочек.
    """
    for entry in suggestions(payload):
        return dict(entry, destination="session")
    if tool_name in SHELL_TOOLS:
        command = str(tool_input.get("command") or "").strip()
        if command and "*" not in command and "\n" not in command and len(command) <= 1000:
            return {"type": "addRules",
                    "rules": [{"toolName": tool_name, "ruleContent": command}],
                    "behavior": "allow", "destination": "session"}
    return None


# --------------------------------------------------------------------------- #
# Безопасный список
# --------------------------------------------------------------------------- #

def harmless_arg(arg):
    """Флаг, который ничего не меняет и не уводит за пределы репозитория."""
    is_flag = arg.startswith("-") or bool(re.match(r"^/[A-Za-z?]$", arg))
    return is_flag and not DENY_FLAG.match(arg)


def safelisted(tool_name, tool_input, path=None):
    """True, если команду можно одобрить сразу, не беспокоя Telegram.

    Строка списка без звёздочки допускает после себя только безобидные флаги:
    «git branch» пропустит «git branch -a», но не «git branch -D main» и не
    «git branch новая». Строка со звёздочкой в конце («npm test *») пропускает
    любые аргументы — это осознанное решение того, кто её добавил.
    """
    try:
        if tool_name not in SHELL_TOOLS:
            return False
        command = str(tool_input.get("command") or "").strip()
        if not command or any(ch in command for ch in UNSAFE_CHARS):
            return False
        norm = re.sub(r"\s+", " ", command)
        low = norm.lower()
        path = path or (tg.HERE / "safelist.txt")
        if not path.exists():
            return False
        for line in path.read_text(encoding="utf-8").splitlines():
            pattern = re.sub(r"\s+", " ", line.strip()).lower()
            if not pattern or pattern.startswith("#"):
                continue
            wildcard = pattern.endswith(" *")
            base = pattern[:-2].strip() if wildcard else pattern
            if low == base:
                return True
            if not low.startswith(base + " "):
                continue
            rest = norm[len(base) + 1:].split()
            if wildcard or all(harmless_arg(arg) for arg in rest):
                return True
    except Exception as exc:
        tg.debug("безопасный список не прочитан: %s" % exc)
    return False


# --------------------------------------------------------------------------- #
# Решение для Claude Code
# --------------------------------------------------------------------------- #

def emit(behavior, message=None, updates=None):
    decision = {"behavior": behavior}
    if message:
        decision["message"] = message
    if updates:
        decision["updatedPermissions"] = updates if isinstance(updates, list) else [updates]
    out = {"hookSpecificOutput": {"hookEventName": HOOK_EVENT, "decision": decision}}
    # ensure_ascii по умолчанию: вывод чистый ASCII, безопасен в любой консоли.
    sys.stdout.write(json.dumps(out))
    sys.stdout.flush()


def keyboard_for(rid, always_label, with_session):
    rows = [[
        {"text": "✅ Разрешить", "callback_data": "a:%s" % rid},
        {"text": "❌ Запретить", "callback_data": "d:%s" % rid},
    ]]
    second = []
    if with_session:
        second.append({"text": "⏳ До конца сессии", "callback_data": "s:%s" % rid})
    second.append({"text": "✏️ Запретить с причиной", "callback_data": "x:%s" % rid})
    rows.append(second)
    if always_label:
        rows.append([{"text": tg.clip("♾ Всегда: %s" % always_label, 48),
                      "callback_data": "r:%s" % rid}])
    return rows


def confirm_keyboard(rid, always_label):
    return [[
        {"text": tg.clip("♾ Да, навсегда: %s" % always_label, 48),
         "callback_data": "R:%s" % rid},
    ], [
        {"text": "⬅ Отмена", "callback_data": "c:%s" % rid},
    ]]


class Ask(object):
    """Всё, что нужно знать о заданном вопросе, в одном месте."""

    def __init__(self, **fields):
        self.__dict__.update(fields)


def finish(ask, behavior, footer, sticker_name, via, press=None, toast="",
           message=None, updates=None, **extra):
    """Окончательное решение: сначала Claude Code, потом всё остальное.

    Раньше сообщение в Telegram переписывалось до печати решения, и сбой
    правки (например, «message is not modified») съедал уже принятое решение.
    """
    emit(behavior, message=message, updates=updates)
    tg.log(dict(ask.base, decision=behavior, via=via, rid=ask.rid,
                detail=ask.detail, waited=int(time.time() - ask.started), **extra))
    try:
        chat = (press or {}).get("chat_id") or ask.chat
        msg = (press or {}).get("message_id") or ask.msg
        if press:
            tg.answer_press(ask.token, press.get("callback_id"), toast)
        pic(sticker_name)
        tg.edit(ask.token, chat, msg, ask.text + "\n\n" + footer)
    except Exception as exc:
        tg.debug("косметика после решения не удалась: %s" % exc)
    return True


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
    if not tg.enabled(env):
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

    # Безопасный список решает на месте и никого не беспокоит, поэтому
    # действует и в тихие часы.
    if safelisted(tool_name, tool_input):
        emit("allow")
        tg.log(dict(base, decision="allow", via="safelist", detail=detail))
        pic("safelist", quiet=True, mode=("all" if _SMODE == "all" else "off"))
        return

    # Тихие часы: ночью не будим, вопрос уходит в терминал.
    if tg.quiet_now(env.get("CC_TG_QUIET")):
        tg.log(dict(base, decision="none", via="quiet_hours", detail=detail))
        return

    tg.prepare_state_dir()
    tg.sweep_pending(token)          # убрать кнопки у брошенных вопросов

    rid = uuid.uuid4().hex[:8]
    always = always_update(payload, tool_name, tool_input)
    session_rule = session_update(payload, tool_name, tool_input)
    always_label = always[0] if always else None
    text = build_text(payload, rid, always_label)
    keyboard = keyboard_for(rid, always_label, bool(session_rule))

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
    # Если ответят в терминале, нас убьют и кнопки останутся висеть —
    # запись позволит следующему запуску их убрать.
    tg.pending_put(rid, chat_for_edit, message_id, text)

    ask = Ask(token=token, chat_id=chat_id, chat=chat_for_edit, msg=message_id,
              rid=rid, text=text, base=base, detail=detail, started=time.time(),
              always=always, session_rule=session_rule)
    deadline = time.time() + wait_timeout
    try:
        decided = handle(ask, deadline)
    finally:
        tg.pending_drop(rid)
    if not decided:
        tg.log(dict(base, decision="none", via="timeout", rid=rid, detail=detail,
                    waited=int(time.time() - ask.started)))
        tg.edit(token, chat_for_edit, message_id,
                text + "\n\n⏳ Время истекло в %s — вопрос ушёл в терминал"
                % time.strftime("%H:%M"))
        pic("timeout")


def handle(ask, deadline):
    """Ждёт и обрабатывает нажатия, пока не будет окончательного решения.

    Возвращает True, если решение напечатано, и False, если истекло время.
    «Всегда» требует подтверждения, поэтому цикл: подтвердили — решаем,
    отменили — возвращаем исходные кнопки и ждём снова.
    """
    stamp = lambda: time.strftime("%H:%M")
    while time.time() < deadline:
        kind, data = tg.wait(ask.token, ask.chat_id, deadline, rid=ask.rid)
        if kind != "press":
            return False
        action = data.get("action")
        chat = data.get("chat_id") or ask.chat
        msg = data.get("message_id") or ask.msg

        if action == "a":
            return finish(ask, "allow", "✅ Разрешено в %s" % stamp(), "allowed",
                          "telegram", press=data, toast="Разрешено")

        if action == "s" and ask.session_rule:
            return finish(ask, "allow", "⏳ Разрешено до конца сессии в %s" % stamp(),
                          "session_allowed", "telegram_session", press=data,
                          toast="Разрешено до конца сессии",
                          updates=ask.session_rule)

        if action == "d":
            return finish(ask, "deny", "❌ Запрещено в %s" % stamp(), "denied",
                          "telegram", press=data, toast="Запрещено",
                          message="Запрещено из Telegram")

        if action == "x":
            tg.answer_press(ask.token, data.get("callback_id"), "Жду причину")
            pic("waiting_reply", quiet=True)
            return deny_with_reason(ask, chat, msg, deadline)

        if action == "r" and ask.always:
            # Второй шаг: правило действует постоянно, спрашиваем ещё раз.
            tg.answer_press(ask.token, data.get("callback_id"), "Подтвердите правило")
            tg.edit(ask.token, chat, msg,
                    ask.text + "\n\n♾ <b>Подтвердите:</b> правило <code>%s</code> "
                               "останется в настройках проекта"
                    % tg.esc(ask.always[0]))
            tg.edit_keyboard(ask.token, chat, msg,
                             confirm_keyboard(ask.rid, ask.always[0]))
            continue

        if action == "R" and ask.always:
            return finish(ask, "allow",
                          "♾ Разрешено навсегда в %s · правило <code>%s</code>"
                          % (stamp(), tg.esc(ask.always[0])),
                          "always_allowed", "telegram_always", press=data,
                          toast="Разрешено навсегда", updates=ask.always[1],
                          rule=ask.always[0])

        if action == "c":
            tg.answer_press(ask.token, data.get("callback_id"), "Отменено")
            tg.edit(ask.token, chat, msg, ask.text)
            tg.edit_keyboard(ask.token, chat, msg,
                             keyboard_for(ask.rid, ask.always and ask.always[0],
                                          bool(ask.session_rule)))
            continue

        tg.debug("неизвестное действие %r" % action)
    return False


def deny_with_reason(ask, chat, msg, deadline):
    """Просит написать причину отказа; её текст уходит обратно в Claude."""
    tg.edit(ask.token, chat, msg,
            ask.text + "\n\n✏️ <b>Напишите причину отказа</b> ответом на это "
                       "сообщение.\nБез ответа запрет уйдёт без пояснения.")
    limit = min(deadline, time.time() + REASON_WAIT)
    kind, data = tg.wait(ask.token, ask.chat_id, limit, rid=ask.rid,
                         want_text=True, reply_to=msg)
    stamp = time.strftime("%H:%M")

    if kind == "press" and data.get("action") == "a":
        # Передумали и нажали «Разрешить» — уважаем последнее действие.
        return finish(ask, "allow", "✅ Разрешено в %s" % stamp, "allowed",
                      "telegram", press=data, toast="Разрешено")

    reason = ""
    if kind == "text":
        reason = tg.clip((data.get("text") or "").strip(), 400)
    footer = "❌ Запрещено в %s" % stamp
    if reason:
        footer += "\n💬 <i>%s</i>" % tg.esc(reason)
    return finish(ask, "deny", footer, "denied_reason" if reason else "denied",
                  "telegram_reason", press=data if kind == "press" else None,
                  toast="Запрещено", message=reason or "Запрещено из Telegram",
                  reason=reason)


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
