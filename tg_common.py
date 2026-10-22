#!/usr/bin/env python3
"""Общие части телеграм-хуков: настройки, Telegram API, ожидание ответа.

Здесь нет ничего, что знало бы про Claude Code — только работа с Telegram
и с файлами состояния. Модуль написан так, чтобы ни одна функция не бросала
исключение наружу без нужды: хук обязан завершаться тихо.

Используется из tg_approve.py (одобрения), tg_notify.py (уведомление
и ответ с телефона) и tg_session.py (проверка при старте сессии).
"""
import html
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
LOG_PATH = HERE / "decisions.jsonl"
STATE_DIR = Path(
    os.environ.get("TEMP") or os.environ.get("TMP") or "/tmp"
) / "cc-tg-approve"
LOCK_PATH = STATE_DIR / "poll.lock"
OFFSET_PATH = STATE_DIR / "offset.txt"
INBOX_PATH = STATE_DIR / "text-inbox.json"

TG_LIMIT = 4096         # предел Telegram на одно сообщение
CHUNK = 3500            # с запасом на служебные строки и теги
LONG_POLL = 20          # максимум секунд на один вопрос к Telegram
LOCK_STALE = 60         # через столько секунд брошенный флажок снимается
MAILBOX_TTL = 600       # через столько секунд чужие нажатия вычищаются
INBOX_TTL = 300         # столько живёт непрочитанное текстовое сообщение
SESSION_TTL = 86400     # столько живёт кэш разрешений сессии

STICKERS_PATH = HERE / "stickers.json"

ENV_KEYS = (
    "TG_TOKEN", "TG_CHAT_ID", "CC_TG_TIMEOUT", "CC_TG_DEBUG", "CC_TG_QUIET",
    "CC_TG_FOLLOWUP", "CC_TG_FOLLOWUP_WAIT", "CC_TG_FOLLOWUP_MAX",
    "CC_TG_ANSWER_LIMIT", "CC_TG_STICKERS", "CC_TG_ENABLED",
)

# Все события, под которые можно нарисовать стикер. Порядок важен:
# в этом же порядке get_sticker_ids.py раскладывает присланные стикеры.
STICKER_EVENTS = (
    ("ask_bash", "ask"), ("ask_edit", "ask"), ("ask_write", "ask"),
    ("ask_danger", "ask"), ("ask_net", "ask"), ("ask_other", "ask"),
    ("allowed", "decision"), ("denied", "decision"),
    ("denied_reason", "decision"), ("session_allowed", "decision"),
    ("always_allowed", "decision"), ("timeout", "decision"),
    ("safelist", "decision"),
    ("mode_default", "life"), ("mode_auto", "life"), ("mode_plan", "life"),
    ("mode_accept_edits", "life"), ("mode_bypass", "life"),
    ("session_start", "life"), ("done", "life"), ("waiting_reply", "life"),
    ("followup_sent", "life"), ("quiet_hours", "life"), ("error", "life"),
)
STICKER_KINDS = dict(STICKER_EVENTS)

_DEBUG = False


# --------------------------------------------------------------------------- #
# Отладка и журнал
# --------------------------------------------------------------------------- #

def set_debug(flag):
    global _DEBUG
    _DEBUG = bool(flag)


def debug(msg):
    if _DEBUG:
        try:
            sys.stderr.write("[tg] %s\n" % msg)
            sys.stderr.flush()
        except Exception:
            pass


def log(record):
    """Дописывает строку в журнал решений. Молча игнорирует любые проблемы."""
    try:
        record = dict(record)
        record["ts"] = time.strftime("%Y-%m-%d %H:%M:%S")
        with open(LOG_PATH, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception as exc:
        debug("журнал не записан: %s" % exc)


# --------------------------------------------------------------------------- #
# Настройки
# --------------------------------------------------------------------------- #

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
        debug(".env не прочитан: %s" % exc)
    # Переменная окружения перекрывает .env, в том числе пустым значением:
    # TG_TOKEN= отключает отправку, что удобно для проверок и для «выключить».
    for key in ENV_KEYS:
        if key in os.environ:
            data[key] = os.environ[key].strip()
    set_debug(data.get("CC_TG_DEBUG", "0") not in ("0", "", "false", "no"))
    return data


def flag(env, key, default=False):
    value = str(env.get(key, "")).strip().lower()
    if not value:
        return default
    return value not in ("0", "false", "no", "off")


def number(env, key, default, low=None, high=None):
    try:
        value = int(str(env.get(key, "")).strip())
    except (TypeError, ValueError):
        return default
    if low is not None:
        value = max(low, value)
    if high is not None:
        value = min(high, value)
    return value


def hook_timeout(event, path=None):
    """Сколько секунд Claude Code готов ждать этот хук, по его settings.json.

    Нужно, чтобы ожидание ответа не оказалось длиннее лимита: иначе хук
    обрывают на полуслове и ответ пропадает. Вернёт None, если лимит не задан
    или файл не прочитался — тогда ограничивать нечем.
    """
    try:
        path = path or (Path.home() / ".claude" / "settings.json")
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        limits = []
        for entry in (data.get("hooks") or {}).get(event) or []:
            for item in entry.get("hooks") or []:
                value = item.get("timeout")
                if isinstance(value, (int, float)) and value > 0:
                    limits.append(float(value))
        return min(limits) if limits else None
    except Exception as exc:
        debug("лимит хука %s не прочитан: %s" % (event, exc))
        return None


def enabled(env):
    """Включён ли бот вообще.

    Выключатель на один рубильник: CC_TG_ENABLED=0 в .env — и все три хука
    молча ничего не делают, вопросы идут обычным путём в терминал.
    """
    if not flag(env, "CC_TG_ENABLED", True):
        debug("выключено через CC_TG_ENABLED")
        return False
    return bool(env.get("TG_TOKEN")) and bool(env.get("TG_CHAT_ID"))


def quiet_now(spec, now=None):
    """True, если текущее время попадает в тихие часы вида "23:00-08:00"."""
    spec = (spec or "").strip()
    if not spec or "-" not in spec:
        return False
    try:
        start, _, end = spec.partition("-")
        sh, sm = [int(x) for x in start.strip().split(":")]
        eh, em = [int(x) for x in end.strip().split(":")]
    except Exception:
        return False
    now = now if now is not None else time.localtime()
    cur = now.tm_hour * 60 + now.tm_min
    begin, finish = sh * 60 + sm, eh * 60 + em
    if begin == finish:
        return False
    if begin < finish:
        return begin <= cur < finish
    return cur >= begin or cur < finish      # интервал через полночь


# --------------------------------------------------------------------------- #
# Текст
# --------------------------------------------------------------------------- #

def esc(text):
    return html.escape(str(text), quote=False)


def clip(text, limit):
    """Жёсткая обрезка с многоточием."""
    text = str(text)
    return text if len(text) <= limit else text[:limit] + " …"


def shorten(text, limit):
    """Начало текста, обрезанное по границе строки или слова."""
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    cut = text[:limit]
    for sep in ("\n", " "):
        pos = cut.rfind(sep)
        if pos > limit * 0.6:            # рвём по границе, но не слишком рано
            cut = cut[:pos]
            break
    return cut.rstrip() + " …"


def head_lines(text, lines):
    """Первые N строк текста, с многоточием если было больше."""
    all_lines = str(text).splitlines()
    out = "\n".join(all_lines[:lines])
    if len(all_lines) > lines:
        out += "\n…"
    return out


_FENCE = re.compile(r"```[a-zA-Z0-9_+-]*\n?(.*?)```", re.S)
_INLINE_CODE = re.compile(r"`([^`\n]+)`")
_BOLD = re.compile(r"\*\*([^*\n]+)\*\*")
_HEADER = re.compile(r"(?m)^#{1,6}\s+(.+)$")
_BULLET = re.compile(r"(?m)^(\s*)[-*]\s+")
_LINK = re.compile(r"\[([^\]\n]+)\]\((https?://[^)\s]+)\)")
_TABLE_BLOCK = re.compile(r"(?m)(?:^\|.*\|[ \t]*\n?)+")


def md_to_html(text):
    """Markdown-ответ Claude в HTML, который понимает Telegram.

    Поддерживаются только теги из документации Telegram: <b>, <i>, <code>,
    <pre>, <a>, <blockquote>. Таблицы заворачиваются в <pre>, иначе колонки
    на телефоне разъезжаются.
    """
    text = str(text or "")
    stash = []

    def keep(payload):
        stash.append(payload)
        return "\x00%d\x00" % (len(stash) - 1)

    # Блоки кода и таблицы — целиком в моноширинный вид, разметка внутри не нужна
    text = _FENCE.sub(lambda m: keep("<pre>%s</pre>" % esc(m.group(1).rstrip())), text)
    text = _TABLE_BLOCK.sub(
        lambda m: keep("<pre>%s</pre>" % esc(m.group(0).rstrip())), text)
    text = _INLINE_CODE.sub(lambda m: keep("<code>%s</code>" % esc(m.group(1))), text)

    text = esc(text)
    text = _LINK.sub(lambda m: '<a href="%s">%s</a>' % (m.group(2), m.group(1)), text)
    text = _HEADER.sub(lambda m: "<b>%s</b>" % m.group(1), text)
    text = _BOLD.sub(lambda m: "<b>%s</b>" % m.group(1), text)
    text = _BULLET.sub(lambda m: "%s• " % m.group(1), text)

    for index, payload in enumerate(stash):
        text = text.replace("\x00%d\x00" % index, payload)
    return text.strip()


def chunks(text, limit=CHUNK):
    """Режет готовый HTML на части по границам строк, не ломая <pre>.

    Если часть обрывается внутри <pre>, блок закрывается и открывается
    заново в следующей части — иначе Telegram отвергнет разметку.
    """
    text = str(text or "")
    if not text.strip():
        return []
    if len(text) <= limit:
        return [text]

    parts, current, size = [], [], 0
    depth = 0                 # сколько <pre> открыто и ещё не закрыто
    carry = False             # начался ли текущий кусок внутри <pre>

    def close():
        """Закрывает набранный кусок, при необходимости достраивая <pre>."""
        nonlocal current, size, carry
        if not current:
            return
        body = "\n".join(current)
        if carry:
            body = "<pre>" + body
        if depth > 0:
            body += "</pre>"
        parts.append(body)
        carry = depth > 0     # следующий кусок продолжает тот же блок
        current, size = [], 0

    for raw in text.split("\n"):
        # очень длинная строка без переносов: режем по символам
        pieces = [raw[i:i + limit] for i in range(0, len(raw), limit)] or [""]
        for line in pieces:
            if current and size + len(line) + 1 > limit:
                close()
            current.append(line)
            size += len(line) + 1
            depth += line.count("<pre>") - line.count("</pre>")
            depth = max(0, depth)
    close()
    return parts


# --------------------------------------------------------------------------- #
# Telegram API
# --------------------------------------------------------------------------- #

class TgFail(Exception):
    def __init__(self, code=0, detail=""):
        super().__init__(detail or str(code))
        self.code = code


def api(token, method, payload, timeout=30):
    """Вызов метода Telegram. При любой неудаче поднимает TgFail."""
    url = "https://api.telegram.org/bot%s/%s" % (token, method)
    body = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        url, data=body, headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as resp:
            answer = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise TgFail(exc.code, "%s -> HTTP %s" % (method, exc.code))
    except Exception as exc:
        raise TgFail(0, "%s -> %s" % (method, exc))
    if not answer.get("ok"):
        raise TgFail(0, "%s -> %s" % (method, answer.get("description")))
    return answer.get("result")


def send(token, chat_id, text, keyboard=None, quiet=False, timeout=20):
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
        "disable_notification": bool(quiet),
    }
    if keyboard:
        payload["reply_markup"] = {"inline_keyboard": keyboard}
    return api(token, "sendMessage", payload, timeout=timeout)


def send_parts(token, chat_id, text, keyboard=None, quiet=False):
    """Отправляет текст, при необходимости несколькими сообщениями.

    Кнопки вешаются на последнее сообщение. Возвращает его же.
    """
    pieces = chunks(text) or [""]
    last = None
    for index, piece in enumerate(pieces):
        is_last = index == len(pieces) - 1
        head = "" if index == 0 else "<i>…продолжение %d/%d</i>\n" % (
            index + 1, len(pieces))
        last = send(token, chat_id, head + piece,
                    keyboard=keyboard if is_last else None, quiet=quiet)
    return last


def edit(token, chat_id, message_id, text, timeout=15):
    """Переписывает сообщение. reply_markup не передаём — кнопки уходят."""
    if not message_id:
        return None
    return api(token, "editMessageText", {
        "chat_id": chat_id,
        "message_id": message_id,
        "text": clip(text, TG_LIMIT - 1),
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }, timeout=timeout)


def edit_keyboard(token, chat_id, message_id, keyboard, timeout=15):
    """Меняет только кнопки, текст оставляет как есть."""
    if not message_id:
        return None
    return api(token, "editMessageReplyMarkup", {
        "chat_id": chat_id,
        "message_id": message_id,
        "reply_markup": {"inline_keyboard": keyboard},
    }, timeout=timeout)


def upload(token, method, fields, field, filename, blob, ctype, timeout=90):
    """Загрузка файла в Telegram (multipart). Нужна только помощникам.

    Telegram принимает PNG 512×512 прямо в sendSticker и возвращает file_id,
    поэтому собирать пак через @Stickers и конвертировать в WEBP не требуется.
    """
    import uuid as _uuid
    boundary = "----cc" + _uuid.uuid4().hex
    body = bytearray()
    for key, value in fields.items():
        body += ("--%s\r\nContent-Disposition: form-data; name=\"%s\"\r\n\r\n%s\r\n"
                 % (boundary, key, value)).encode("utf-8")
    body += ("--%s\r\nContent-Disposition: form-data; name=\"%s\"; "
             "filename=\"%s\"\r\nContent-Type: %s\r\n\r\n"
             % (boundary, field, filename, ctype)).encode("utf-8")
    body += blob + ("\r\n--%s--\r\n" % boundary).encode("utf-8")

    request = urllib.request.Request(
        "https://api.telegram.org/bot%s/%s" % (token, method), data=bytes(body),
        headers={"Content-Type": "multipart/form-data; boundary=%s" % boundary})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as resp:
            answer = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        try:
            detail = json.loads(exc.read().decode("utf-8")).get("description")
        except Exception:
            detail = "HTTP %s" % exc.code
        raise TgFail(exc.code, "%s -> %s" % (method, detail))
    except Exception as exc:
        raise TgFail(0, "%s -> %s" % (method, exc))
    if not answer.get("ok"):
        raise TgFail(0, "%s -> %s" % (method, answer.get("description")))
    return answer.get("result")


def load_stickers(path=None):
    """Карта событие -> file_id из stickers.json. Пустые значения отбрасываем."""
    try:
        data = json.loads((path or STICKERS_PATH).read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return {}
        return {str(k): str(v).strip() for k, v in data.items()
                if isinstance(v, str) and v.strip() and not k.startswith("_")}
    except Exception:
        return {}


def sticker_mode(env, stickers=None):
    """off — не отправлять, decisions — только итоги, all — всё.

    По умолчанию: all, если в stickers.json есть хоть один file_id, иначе off.
    """
    value = str((env or {}).get("CC_TG_STICKERS", "")).strip().lower()
    if value in ("off", "0", "false", "no"):
        return "off"
    if value in ("all", "decisions"):
        return value
    if value in ("on", "1", "true", "yes"):
        return "all"
    have = load_stickers() if stickers is None else stickers
    return "all" if have else "off"


def sticker(token, chat_id, name, mode="off", quiet=False, stickers=None):
    """Отправляет стикер события, если он задан и режим это позволяет.

    Любая ошибка глотается: стикер — украшение, из-за него хук падать не должен.
    """
    if mode == "off" or not name:
        return False
    kind = STICKER_KINDS.get(name, "life")
    if mode == "decisions" and kind != "decision":
        return False
    table = load_stickers() if stickers is None else stickers
    file_id = table.get(name)
    if not file_id:
        return False
    try:
        api(token, "sendSticker", {
            "chat_id": chat_id,
            "sticker": file_id,
            "disable_notification": bool(quiet),
        }, timeout=10)
        return True
    except TgFail as exc:
        debug("стикер %s не отправлен: %s" % (name, exc))
        return False


def answer_press(token, callback_id, text="", alert=False, timeout=10):
    """Убирает «часики» на кнопке. Ошибку глотаем — это косметика."""
    if not callback_id:
        return
    try:
        api(token, "answerCallbackQuery", {
            "callback_query_id": callback_id,
            "text": text,
            "show_alert": bool(alert),
        }, timeout=timeout)
    except TgFail as exc:
        debug("нажатие не подтверждено: %s" % exc)


# --------------------------------------------------------------------------- #
# Файлы состояния: флажок, полочка, входящие тексты, кэш сессии
# --------------------------------------------------------------------------- #

def prepare_state_dir():
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        now = time.time()
        for pattern, ttl in (("cb-*.json", MAILBOX_TTL),
                             ("session-*.json", SESSION_TTL)):
            for item in STATE_DIR.glob(pattern):
                try:
                    if now - item.stat().st_mtime > ttl:
                        item.unlink()
                except OSError:
                    pass
    except Exception as exc:
        debug("рабочая папка не готова: %s" % exc)


def pid_alive(pid):
    """Жив ли процесс. Работает и на Windows, и на POSIX."""
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    if os.name == "nt":
        import subprocess
        try:
            out = subprocess.run(
                ["tasklist", "/FI", "PID eq %d" % pid, "/NH"],
                capture_output=True, text=True, timeout=5,
                creationflags=0x08000000,          # без мигающего окна консоли
            ).stdout
            return str(pid) in out
        except Exception:
            return True          # не смогли проверить — считаем живым
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except Exception:
        return True
    return True


def take_lock():
    """Берёт флажок «я опрашиваю Telegram». Брошенный флажок снимает сам."""
    try:
        fd = os.open(str(LOCK_PATH), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        try:
            os.write(fd, ("%d" % os.getpid()).encode("ascii"))
        finally:
            os.close(fd)
        return True
    except FileExistsError:
        try:
            owner = LOCK_PATH.read_text(encoding="ascii").strip()
            stale = time.time() - LOCK_PATH.stat().st_mtime > LOCK_STALE
            if stale or not pid_alive(owner):
                LOCK_PATH.unlink()
                debug("снял флажок процесса %s" % owner)
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
        debug("номер письма не сохранён: %s" % exc)


def _write_json(path, payload):
    tmp = path.with_suffix(path.suffix + ".%d.tmp" % os.getpid())
    tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    os.replace(str(tmp), str(path))


def mailbox_put(rid, press):
    """Кладёт на полочку нажатие, адресованное другому процессу."""
    if not re.match(r"^[0-9a-f]{4,16}$", str(rid or "")):
        return
    try:
        _write_json(STATE_DIR / ("cb-%s.json" % rid), press)
        debug("на полочку: нажатие для %s" % rid)
    except Exception as exc:
        debug("на полочку не положил: %s" % exc)


def mailbox_take(rid):
    target = STATE_DIR / ("cb-%s.json" % rid)
    try:
        if not target.exists():
            return None
        press = json.loads(target.read_text(encoding="utf-8"))
        target.unlink()
        debug("с полочки: своё нажатие")
        return press
    except Exception as exc:
        debug("с полочки не забрал: %s" % exc)
        return None


def inbox_put(message):
    """Складывает текстовое сообщение из Telegram в общие входящие."""
    try:
        items = _inbox_read()
        items.append(message)
        now = time.time()
        items = [m for m in items if now - m.get("ts", now) < INBOX_TTL]
        _write_json(INBOX_PATH, items[-20:])
        debug("во входящие: текст %r" % (message.get("text", "")[:40],))
    except Exception as exc:
        debug("во входящие не положил: %s" % exc)


def _inbox_read():
    try:
        items = json.loads(INBOX_PATH.read_text(encoding="utf-8"))
        return items if isinstance(items, list) else []
    except Exception:
        return []


def inbox_take(since, reply_to=None):
    """Забирает текстовое сообщение: ответ на наше сообщение либо самое старое.

    Сообщения старше `since` игнорируются, чтобы не подхватить чужую реплику,
    написанную до того, как мы начали ждать.
    """
    try:
        items = _inbox_read()
        if not items:
            return None
        fresh = [m for m in items if m.get("ts", 0) >= since]
        picked = None
        if reply_to:
            for m in fresh:
                if m.get("reply_to") == reply_to:
                    picked = m
                    break
        if picked is None and fresh:
            picked = fresh[0]
        if picked is None:
            return None
        items.remove(picked)
        _write_json(INBOX_PATH, items)
        return picked
    except Exception as exc:
        debug("из входящих не забрал: %s" % exc)
        return None


def pending_put(rid, chat_id, message_id, text):
    """Запоминает отправленный вопрос, чтобы потом убрать у него кнопки.

    Если ответить в терминале, Claude Code убивает процесс хука и переписать
    сообщение тот уже не успевает. Запись остаётся — её подберёт следующий
    запуск и уберёт кнопки у вопроса, который давно решён.
    """
    try:
        _write_json(STATE_DIR / ("pending-%s.json" % rid),
                    {"pid": os.getpid(), "chat_id": chat_id,
                     "message_id": message_id, "text": text, "ts": time.time()})
    except Exception as exc:
        debug("вопрос не записан: %s" % exc)


def pending_drop(rid):
    try:
        (STATE_DIR / ("pending-%s.json" % rid)).unlink()
    except OSError:
        pass


def sweep_pending(token, note="⌨️ Решено в терминале", max_age=900):
    """Убирает кнопки у вопросов, чьи процессы уже не живы."""
    try:
        mine = os.getpid()
        for item in list(STATE_DIR.glob("pending-*.json")):
            try:
                data = json.loads(item.read_text(encoding="utf-8"))
            except Exception:
                item.unlink()
                continue
            pid = data.get("pid")
            age = time.time() - float(data.get("ts") or 0)
            if pid == mine:
                continue
            # Номер процесса система может выдать заново, поэтому старые
            # записи подчищаем в любом случае.
            if pid and pid_alive(pid) and age < max_age:
                continue
            try:
                edit(token, data.get("chat_id"), data.get("message_id"),
                     (data.get("text") or "") + "\n\n" + note)
                debug("убрал кнопки у брошенного вопроса %s" % item.stem)
            except TgFail as exc:
                debug("не удалось убрать кнопки: %s" % exc)
            item.unlink()
    except Exception as exc:
        debug("уборка брошенных вопросов не удалась: %s" % exc)


def session_path(session_id):
    safe = re.sub(r"[^0-9a-zA-Z_-]", "", str(session_id or "none"))[:64]
    return STATE_DIR / ("session-%s.json" % (safe or "none"))


def session_allowed(session_id, key):
    """Разрешено ли это действие до конца сессии (кнопка «⏳»)."""
    try:
        data = json.loads(session_path(session_id).read_text(encoding="utf-8"))
        return key in (data.get("keys") or [])
    except Exception:
        return False


def session_allow(session_id, key):
    path = session_path(session_id)
    try:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            data = {}
        keys = data.get("keys") or []
        if key not in keys:
            keys.append(key)
        _write_json(path, {"keys": keys[-200:]})
        debug("до конца сессии разрешено: %s" % key)
    except Exception as exc:
        debug("кэш сессии не записан: %s" % exc)


# --------------------------------------------------------------------------- #
# Ожидание ответа из Telegram
# --------------------------------------------------------------------------- #

def parse_press(callback, chat_id, token):
    """Разбор нажатия. Возвращает (номер запроса, данные) или (None, None)."""
    sender = str((callback.get("from") or {}).get("id") or "")
    if sender != str(chat_id):
        debug("нажатие от посторонних (%s)" % sender)
        answer_press(token, callback.get("id"), "Нет доступа", alert=True)
        return None, None
    action, _, rid = str(callback.get("data") or "").partition(":")
    if not action or not rid:
        return None, None
    message = callback.get("message") or {}
    return rid, {
        "action": action,
        "callback_id": callback.get("id"),
        "message_id": message.get("message_id"),
        "chat_id": (message.get("chat") or {}).get("id"),
    }


def parse_text(message, chat_id):
    """Разбор текстового сообщения от владельца. None, если не подходит."""
    sender = str((message.get("from") or {}).get("id") or "")
    if sender != str(chat_id):
        return None
    text = (message.get("text") or "").strip()
    if not text:
        return None
    reply = (message.get("reply_to_message") or {}).get("message_id")
    # Время отправки, а не получения: иначе сообщение, написанное до того, как
    # мы начали ждать, подхватится следующим ожиданием как свежий ответ.
    try:
        sent_at = float(message.get("date") or 0) or time.time()
    except (TypeError, ValueError):
        sent_at = time.time()
    return {"ts": sent_at, "text": text,
            "message_id": message.get("message_id"), "reply_to": reply}


def close_orphan_press(token, press, note="⌨️ Этот вопрос уже закрыт"):
    """Отвечает на нажатие, которого никто не ждёт, и убирает кнопки.

    Так бывает, когда на вопрос ответили в терминале или хук уже завершился:
    процесса нет, отвечать некому, и Telegram бесконечно крутит спиннер.
    """
    answer_press(token, press.get("callback_id"), "Вопрос уже закрыт")
    try:
        edit_keyboard(token, press.get("chat_id"), press.get("message_id"), [])
        debug("закрыл осиротевшее нажатие")
    except TgFail as exc:
        debug("осиротевшее нажатие не закрыть: %s" % exc)


def is_pending(rid):
    """Ждёт ли кто-то ответа по этому запросу."""
    try:
        return (STATE_DIR / ("pending-%s.json" % rid)).exists()
    except Exception:
        return False


def wait(token, chat_id, deadline, rid=None, want_text=False, reply_to=None,
         since=None):
    """Ждёт нажатие кнопки с номером `rid` и/или текстовый ответ.

    Возвращает ("press", данные) | ("text", данные) | (None, None) по таймауту.
    К Telegram в один момент обращается только один процесс (флажок);
    чужие нажатия он раскладывает по полочке, тексты — во входящие.
    """
    since = time.time() if since is None else since
    while time.time() < deadline:
        if rid:
            press = mailbox_take(rid)
            if press:
                return "press", press
        if want_text:
            message = inbox_take(since, reply_to)
            if message:
                return "text", message

        if not take_lock():
            time.sleep(1)
            continue
        try:
            left = deadline - time.time()
            if left <= 0:
                return None, None
            offset = read_offset()
            if offset is None:
                # Первый запуск: берём только самое свежее письмо, не всю историю.
                params = {"offset": -1, "timeout": 0}
            else:
                params = {"offset": offset,
                          "timeout": max(1, min(LONG_POLL, int(left)))}
            params["allowed_updates"] = ["callback_query", "message"]
            try:
                updates = api(token, "getUpdates", params,
                              timeout=params["timeout"] + 20) or []
            except TgFail as exc:
                debug("getUpdates не удался: %s" % exc)
                time.sleep(2)
                continue

            mine = None
            mine_text = None
            for update in updates:
                if "update_id" in update:
                    write_offset(update["update_id"] + 1)
                callback = update.get("callback_query")
                if callback:
                    got, press = parse_press(callback, chat_id, token)
                    if not got:
                        continue
                    if rid and got == rid:
                        mine = press
                    elif is_pending(got):
                        mailbox_put(got, press)      # ждёт другой процесс
                    else:
                        close_orphan_press(token, press)
                    continue
                message = update.get("message")
                if message:
                    parsed = parse_text(message, chat_id)
                    if not parsed:
                        continue
                    if handle_command(parsed["text"], token, chat_id):
                        continue          # это команда, а не ответ
                    if (want_text and mine_text is None
                            and parsed.get("ts", 0) >= since - 1
                            and (not reply_to
                                 or parsed.get("reply_to") in (None, reply_to))):
                        mine_text = parsed
                    else:
                        inbox_put(parsed)
            if mine:
                return "press", mine
            if mine_text:
                return "text", mine_text
        finally:
            drop_lock()
    return None, None


# --------------------------------------------------------------------------- #
# Выключатель: команды из Telegram и правка .env
# --------------------------------------------------------------------------- #

ENV_PATH = HERE / ".env"
FAIL_PATH = STATE_DIR / "poll-fail.txt"
BACKOFF = 600           # столько молчим после неудачной попытки связи


def set_enabled(value):
    """Переписывает CC_TG_ENABLED в .env. Единственное место правды."""
    line = "CC_TG_ENABLED=%d" % (1 if value else 0)
    try:
        text = ENV_PATH.read_text(encoding="utf-8") if ENV_PATH.exists() else ""
        rows, done = [], False
        for row in text.splitlines():
            if row.strip().startswith("CC_TG_ENABLED="):
                rows.append(line)
                done = True
            else:
                rows.append(row)
        if not done:
            rows.append(line)
        ENV_PATH.write_text("\n".join(rows) + "\n", encoding="utf-8")
        return True
    except Exception as exc:
        debug("выключатель не записан: %s" % exc)
        return False


def handle_command(text, token, chat_id):
    """Обрабатывает /on, /off, /status. True — это была команда."""
    word = (text or "").strip().split(" ")[0].split("@")[0].lower()
    if not word.startswith("/"):
        return False
    env = read_env()
    now = enabled(env)

    if word in ("/on", "/start"):
        set_enabled(True)
        answer = ("✅ Включён — вопросы снова приходят сюда."
                  if not now else "✅ И так был включён.")
    elif word == "/off":
        set_enabled(False)
        answer = ("🔕 Выключен — вопросы идут в терминал.\nВернуть: /on"
                  if now else "🔕 И так был выключен.\nВернуть: /on")
    elif word == "/status":
        answer = "%s\nТихие часы: %s" % (
            "✅ Включён" if now else "🔕 Выключен",
            env.get("CC_TG_QUIET") or "не заданы")
    else:
        answer = "Команды: /on — включить, /off — выключить, /status — состояние"
    log({"tool": "command", "via": "command", "command": word,
         "enabled_before": now, "enabled_after": enabled(read_env())})
    try:
        send(token, chat_id, answer, quiet=True)
    except TgFail as exc:
        debug("ответ на команду не ушёл: %s" % exc)
    return True


def poll_commands(token, chat_id, timeout=5):
    """Короткий загляд в Telegram: ищем только команды.

    Нужен, когда бот выключен: иначе включить его с телефона было бы нечем.
    После неудачи молчим BACKOFF секунд, чтобы отсутствие сети не замедляло
    каждый ход.
    """
    try:
        if time.time() - float(FAIL_PATH.read_text(encoding="ascii")) < BACKOFF:
            return False
    except Exception:
        pass
    if not take_lock():
        return False
    try:
        offset = read_offset()
        # Просить только сообщения нельзя: подтверждение номера стирает из
        # очереди и нажатия кнопок, которых мы бы не увидели.
        params = {"timeout": 0, "allowed_updates": ["callback_query", "message"],
                  "offset": offset if offset is not None else -1}
        try:
            updates = api(token, "getUpdates", params, timeout=timeout) or []
        except TgFail as exc:
            debug("загляд за командами не удался: %s" % exc)
            try:
                FAIL_PATH.write_text(str(time.time()), encoding="ascii")
            except Exception:
                pass
            return False
        try:
            FAIL_PATH.unlink()
        except OSError:
            pass
        acted = False
        for update in updates:
            if "update_id" in update:
                write_offset(update["update_id"] + 1)
            callback = update.get("callback_query")
            if callback:
                got, press = parse_press(callback, chat_id, token)
                if got and is_pending(got):
                    mailbox_put(got, press)      # ждёт живой процесс
                elif got:
                    close_orphan_press(token, press)
                continue
            parsed = parse_text(update.get("message") or {}, chat_id)
            if not parsed:
                continue
            if handle_command(parsed["text"], token, chat_id):
                acted = True
            else:
                inbox_put(parsed)                # вдруг это ответ, пусть полежит
        return acted
    finally:
        drop_lock()
