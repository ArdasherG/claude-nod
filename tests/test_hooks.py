#!/usr/bin/env python3
"""Автотесты телеграм-хуков. Запуск из корня проекта:

    python -m unittest discover -s tests -v

Сеть не используется: всё, что ходит в Telegram, подменяется заглушками.
Файлы состояния пишутся во временную папку, настоящие не трогаются.
"""
import io
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import tg_common as tg          # noqa: E402
import tg_approve as ap         # noqa: E402
import tg_notify as nt          # noqa: E402
import tg_session as ss         # noqa: E402


class StateCase(unittest.TestCase):
    """База для тестов, которым нужны файлы состояния во временной папке."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.saved = {
            "STATE_DIR": tg.STATE_DIR, "LOCK_PATH": tg.LOCK_PATH,
            "OFFSET_PATH": tg.OFFSET_PATH, "INBOX_PATH": tg.INBOX_PATH,
            "LOG_PATH": tg.LOG_PATH, "STICKERS_PATH": tg.STICKERS_PATH,
        }
        tg.STATE_DIR = root
        tg.LOCK_PATH = root / "poll.lock"
        tg.OFFSET_PATH = root / "offset.txt"
        tg.INBOX_PATH = root / "text-inbox.json"
        tg.LOG_PATH = root / "decisions.jsonl"
        tg.STICKERS_PATH = root / "stickers.json"

    def tearDown(self):
        for name, value in self.saved.items():
            setattr(tg, name, value)
        self.tmp.cleanup()


# --------------------------------------------------------------------------- #
# Безопасный список
# --------------------------------------------------------------------------- #

class SafelistTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(
            "w", suffix=".txt", delete=False, encoding="utf-8")
        self.tmp.write("# комментарий\ngit status\nls\npython -V\n\n")
        self.tmp.close()
        self.path = Path(self.tmp.name)

    def tearDown(self):
        os.unlink(self.tmp.name)

    def allowed(self, command, tool="Bash"):
        return ap.safelisted(tool, {"command": command}, path=self.path)

    def test_exact_and_prefix(self):
        self.assertTrue(self.allowed("git status"))
        self.assertTrue(self.allowed("git status --short"))
        self.assertTrue(self.allowed("  GIT   STATUS  "))   # регистр и пробелы
        self.assertTrue(self.allowed("ls -la"))

    def test_not_a_prefix_of_another_word(self):
        self.assertFalse(self.allowed("lsof -i"))
        self.assertFalse(self.allowed("git statuses"))

    def test_chaining_is_refused(self):
        for command in ("git status && rm -rf x", "ls; rm x", "ls | sh",
                        "ls `rm x`", "ls > out", "ls $(rm x)"):
            self.assertFalse(self.allowed(command), command)

    def test_unknown_command(self):
        self.assertFalse(self.allowed("rm -rf dist"))
        self.assertFalse(self.allowed("cat .env"))

    def test_only_shell_tools(self):
        self.assertFalse(self.allowed("git status", tool="Edit"))
        self.assertFalse(ap.safelisted("Write", {"file_path": "a"}, path=self.path))

    def test_missing_file_is_safe(self):
        self.assertFalse(ap.safelisted(
            "Bash", {"command": "git status"}, path=Path("нет-такого-файла.txt")))

    def test_shipped_safelist_has_no_file_readers(self):
        """cat/head/tail не должны вернуться в поставляемый список."""
        shipped = ROOT / "safelist.txt"
        for command in ("cat .env", "head ~/.ssh/id_rsa", "tail secrets.txt"):
            self.assertFalse(
                ap.safelisted("Bash", {"command": command}, path=shipped), command)


# --------------------------------------------------------------------------- #
# Правила «Разрешить всегда»
# --------------------------------------------------------------------------- #

class RuleTest(unittest.TestCase):
    def rule(self, command):
        return ap.derive_rule("Bash", {"command": command}, "C:/p")

    def test_two_words(self):
        self.assertEqual(self.rule("git push origin main"), ("Bash", "git push *"))
        self.assertEqual(self.rule("npm run build"), ("Bash", "npm run *"))

    def test_one_word_when_second_is_flag_or_path(self):
        self.assertEqual(self.rule("pytest -q"), ("Bash", "pytest *"))
        self.assertEqual(self.rule("python script.py"), ("Bash", "python *"))

    def test_dangerous_commands_get_no_rule(self):
        for command in ("rm -rf dist", "del x", "mv a b", "curl http://x",
                        "shutdown /s", "chmod 777 f", "RM.EXE x"):
            self.assertIsNone(self.rule(command), command)

    def test_chaining_and_odd_heads(self):
        self.assertIsNone(self.rule("git status && rm x"))
        self.assertIsNone(self.rule("./gradlew assembleDebug"))
        self.assertIsNone(self.rule(""))

    def test_file_tools_use_folder(self):
        tool, rule = ap.derive_rule("Edit", {"file_path": "x"}, "C:/proj")
        self.assertEqual(tool, "Edit")
        self.assertTrue(rule.endswith("**"))
        self.assertIn("proj", rule)

    def test_unknown_tool(self):
        self.assertIsNone(ap.derive_rule("Task", {}, "C:/p"))


# --------------------------------------------------------------------------- #
# Текст и разметка
# --------------------------------------------------------------------------- #

class TextTest(unittest.TestCase):
    def test_clip_and_shorten(self):
        self.assertEqual(tg.clip("abc", 10), "abc")
        self.assertEqual(tg.clip("a" * 20, 5), "aaaaa …")
        self.assertEqual(tg.shorten("abc", 10), "abc")
        long = "слово " * 100
        out = tg.shorten(long, 50)
        self.assertTrue(out.endswith("…"))
        self.assertLessEqual(len(out), 52)
        self.assertTrue(out.startswith("слово"))

    def test_head_lines(self):
        self.assertEqual(tg.head_lines("a\nb\nc", 2), "a\nb\n…")
        self.assertEqual(tg.head_lines("a\nb", 5), "a\nb")

    def test_escaping(self):
        self.assertEqual(tg.esc("a < b & c"), "a &lt; b &amp; c")

    def test_md_code_is_escaped(self):
        out = tg.md_to_html("```\nif a < b & c:\n```")
        self.assertIn("<pre>", out)
        self.assertIn("a &lt; b &amp; c", out)
        self.assertNotIn("a < b", out)

    def test_md_inline_markup(self):
        out = tg.md_to_html("## Шапка\n**жирный** и `код`\n- пункт")
        self.assertIn("<b>Шапка</b>", out)
        self.assertIn("<b>жирный</b>", out)
        self.assertIn("<code>код</code>", out)
        self.assertIn("• пункт", out)

    def test_md_link_and_table(self):
        out = tg.md_to_html("[тут](https://e.com/a?b=1)\n\n| a | b |\n|---|---|\n| 1 | 2 |")
        self.assertIn('<a href="https://e.com/a?b=1">тут</a>', out)
        self.assertIn("<pre>| a | b |", out)

    def test_md_no_markup_inside_code(self):
        out = tg.md_to_html("`**не жирный**`")
        self.assertIn("<code>**не жирный**</code>", out)
        self.assertNotIn("<b>", out)

    def test_chunks_respect_limit(self):
        body = "\n".join("строка %03d с наполнителем" % i for i in range(300))
        parts = tg.chunks(body, limit=400)
        self.assertGreater(len(parts), 1)
        for part in parts:
            self.assertLessEqual(len(part), 420)

    def test_chunks_keep_pre_balanced(self):
        body = "начало\n<pre>" + "\n".join("код %03d" % i for i in range(80)) + "</pre>\nконец"
        for part in tg.chunks(body, limit=300):
            self.assertEqual(part.count("<pre>"), part.count("</pre>"), part[:50])

    def test_chunks_lose_nothing(self):
        body = "\n".join("строка %d" % i for i in range(100))
        joined = "\n".join(tg.chunks(body, limit=200))
        for i in (0, 42, 99):
            self.assertIn("строка %d" % i, joined)

    def test_chunks_long_single_line(self):
        parts = tg.chunks("x" * 1000, limit=100)
        self.assertTrue(all(len(p) <= 100 for p in parts))
        self.assertEqual(sum(p.count("x") for p in parts), 1000)

    def test_chunks_empty(self):
        self.assertEqual(tg.chunks(""), [])
        self.assertEqual(tg.chunks("   "), [])


# --------------------------------------------------------------------------- #
# Настройки
# --------------------------------------------------------------------------- #

class EnvTest(unittest.TestCase):
    def test_flag(self):
        self.assertTrue(tg.flag({"X": "1"}, "X"))
        self.assertTrue(tg.flag({"X": "yes"}, "X"))
        self.assertFalse(tg.flag({"X": "0"}, "X"))
        self.assertFalse(tg.flag({"X": "off"}, "X"))
        self.assertFalse(tg.flag({}, "X"))
        self.assertTrue(tg.flag({}, "X", default=True))

    def test_number_clamped(self):
        self.assertEqual(tg.number({"N": "50"}, "N", 10), 50)
        self.assertEqual(tg.number({"N": "2"}, "N", 10, low=5), 5)
        self.assertEqual(tg.number({"N": "9999"}, "N", 10, high=100), 100)
        self.assertEqual(tg.number({"N": "мусор"}, "N", 10), 10)
        self.assertEqual(tg.number({}, "N", 10), 10)

    def test_env_overrides_dotenv_even_when_empty(self):
        """TG_TOKEN= в окружении обязан выключить отправку."""
        saved = os.environ.get("TG_TOKEN")
        os.environ["TG_TOKEN"] = ""
        try:
            self.assertEqual(tg.read_env().get("TG_TOKEN"), "")
        finally:
            if saved is None:
                del os.environ["TG_TOKEN"]
            else:
                os.environ["TG_TOKEN"] = saved

    def test_quiet_hours(self):
        mk = lambda h, m=0: time.struct_time((2026, 10, 21, h, m, 0, 2, 294, 0))
        self.assertTrue(tg.quiet_now("23:00-08:00", mk(23, 30)))
        self.assertTrue(tg.quiet_now("23:00-08:00", mk(2)))
        self.assertFalse(tg.quiet_now("23:00-08:00", mk(8)))
        self.assertFalse(tg.quiet_now("23:00-08:00", mk(12)))
        self.assertTrue(tg.quiet_now("09:00-18:00", mk(12)))
        self.assertFalse(tg.quiet_now("09:00-18:00", mk(20)))
        for bad in ("", None, "мусор", "25:99", "10:00", "10:00-10:00"):
            self.assertFalse(tg.quiet_now(bad, mk(10)), repr(bad))


# --------------------------------------------------------------------------- #
# Разбор ответов из Telegram
# --------------------------------------------------------------------------- #

class ParseTest(unittest.TestCase):
    def setUp(self):
        self.denied = []
        self.saved = tg.answer_press
        tg.answer_press = lambda *a, **k: self.denied.append(a)

    def tearDown(self):
        tg.answer_press = self.saved

    def press(self, data, sender="111"):
        return tg.parse_press(
            {"id": "cb1", "from": {"id": sender}, "data": data,
             "message": {"message_id": 7, "chat": {"id": 111}}}, "111", "tok")

    def test_valid_press(self):
        rid, info = self.press("a:abcd1234")
        self.assertEqual(rid, "abcd1234")
        self.assertEqual(info["action"], "a")
        self.assertEqual(info["message_id"], 7)

    def test_foreign_sender_refused(self):
        rid, info = self.press("a:abcd1234", sender="999")
        self.assertIsNone(rid)
        self.assertIsNone(info)
        self.assertTrue(self.denied, "постороннему должны ответить «нет доступа»")

    def test_broken_data(self):
        for data in ("", "мусор", "a:", ":abcd"):
            rid, _ = self.press(data)
            self.assertIsNone(rid, repr(data))

    def test_text_from_owner(self):
        parsed = tg.parse_text(
            {"from": {"id": "111"}, "text": "  причина  ", "message_id": 9,
             "reply_to_message": {"message_id": 7}}, "111")
        self.assertEqual(parsed["text"], "причина")
        self.assertEqual(parsed["reply_to"], 7)

    def test_text_from_stranger_or_empty(self):
        self.assertIsNone(tg.parse_text({"from": {"id": "999"}, "text": "x"}, "111"))
        self.assertIsNone(tg.parse_text({"from": {"id": "111"}, "text": "  "}, "111"))
        self.assertIsNone(tg.parse_text({"from": {"id": "111"}}, "111"))


# --------------------------------------------------------------------------- #
# Файлы состояния
# --------------------------------------------------------------------------- #

class StateTest(StateCase):
    def test_lock_is_exclusive_and_released(self):
        self.assertTrue(tg.take_lock())
        self.assertFalse(tg.take_lock(), "второй раз флажок брать нельзя")
        tg.drop_lock()
        self.assertTrue(tg.take_lock())
        tg.drop_lock()

    def test_lock_of_dead_process_is_removed(self):
        tg.LOCK_PATH.write_text("999999999", encoding="ascii")
        self.assertFalse(tg.take_lock(), "сначала только снимаем чужой флажок")
        self.assertTrue(tg.take_lock(), "после снятия флажок свободен")
        tg.drop_lock()

    def test_offset_roundtrip(self):
        self.assertIsNone(tg.read_offset())
        tg.write_offset(42)
        self.assertEqual(tg.read_offset(), 42)

    def test_mailbox_roundtrip(self):
        tg.mailbox_put("abcd1234", {"action": "a"})
        self.assertEqual(tg.mailbox_take("abcd1234"), {"action": "a"})
        self.assertIsNone(tg.mailbox_take("abcd1234"), "забирается один раз")

    def test_mailbox_ignores_bad_rid(self):
        tg.mailbox_put("../обход", {"action": "a"})
        tg.mailbox_put("", {"action": "a"})
        self.assertEqual(list(tg.STATE_DIR.glob("cb-*.json")), [])

    def test_mailbox_is_per_request(self):
        tg.mailbox_put("aaaa1111", {"action": "a"})
        self.assertIsNone(tg.mailbox_take("bbbb2222"), "чужое забирать нельзя")

    def test_inbox_order_and_since(self):
        now = time.time()
        tg.inbox_put({"ts": now - 100, "text": "старое", "reply_to": None})
        tg.inbox_put({"ts": now, "text": "новое", "reply_to": None})
        self.assertIsNone(tg.inbox_take(now + 10), "будущее не выдаём")
        taken = tg.inbox_take(now - 50)
        self.assertEqual(taken["text"], "новое", "старое не должно подхватываться")

    def test_inbox_prefers_reply(self):
        now = time.time()
        tg.inbox_put({"ts": now, "text": "общее", "reply_to": None})
        tg.inbox_put({"ts": now, "text": "адресное", "reply_to": 7})
        self.assertEqual(tg.inbox_take(now - 1, reply_to=7)["text"], "адресное")

    def test_session_cache(self):
        key = ap.session_key("Bash", {"command": "git push origin main"})
        self.assertFalse(tg.session_allowed("s1", key))
        tg.session_allow("s1", key)
        self.assertTrue(tg.session_allowed("s1", key))
        self.assertFalse(tg.session_allowed("s2", key), "сессии не делят кэш")

    def test_session_key_normalises(self):
        a = ap.session_key("Bash", {"command": "git   PUSH  origin main"})
        b = ap.session_key("Bash", {"command": "git push origin main"})
        self.assertEqual(a, b)
        self.assertNotEqual(a, ap.session_key("Bash", {"command": "git pull"}))

    def test_session_id_cannot_escape_folder(self):
        path = tg.session_path("../../злой")
        self.assertEqual(path.parent, tg.STATE_DIR)

    def test_log_is_one_line_per_record(self):
        tg.log({"via": "test", "detail": "раз"})
        tg.log({"via": "test", "detail": "два"})
        lines = tg.LOG_PATH.read_text(encoding="utf-8").strip().splitlines()
        self.assertEqual(len(lines), 2)
        self.assertEqual(json.loads(lines[0])["detail"], "раз")
        self.assertIn("ts", json.loads(lines[0]))


# --------------------------------------------------------------------------- #
# Стикеры
# --------------------------------------------------------------------------- #

class StickerTest(StateCase):
    def write_map(self, data):
        tg.STICKERS_PATH.write_text(json.dumps(data), encoding="utf-8")

    def test_mode_off_without_file(self):
        self.assertEqual(tg.sticker_mode({}), "off")

    def test_mode_all_when_filled(self):
        self.write_map({"allowed": "ID1"})
        self.assertEqual(tg.sticker_mode({}), "all")
        self.assertEqual(tg.sticker_mode({"CC_TG_STICKERS": "off"}), "off")
        self.assertEqual(tg.sticker_mode({"CC_TG_STICKERS": "decisions"}), "decisions")

    def test_empty_values_are_ignored(self):
        self.write_map({"allowed": "", "_подсказка": "текст", "denied": "ID2"})
        table = tg.load_stickers()
        self.assertEqual(table, {"denied": "ID2"})

    def test_broken_file_is_safe(self):
        tg.STICKERS_PATH.write_text("{не json", encoding="utf-8")
        self.assertEqual(tg.load_stickers(), {})

    def test_sends_only_known_events(self):
        sent = []
        saved = tg.api
        tg.api = lambda token, method, payload, timeout=30: sent.append(
            (method, payload.get("sticker")))
        try:
            table = {"allowed": "ID1"}
            self.assertTrue(tg.sticker("t", "1", "allowed", "all", stickers=table))
            self.assertFalse(tg.sticker("t", "1", "denied", "all", stickers=table))
            self.assertFalse(tg.sticker("t", "1", "allowed", "off", stickers=table))
        finally:
            tg.api = saved
        self.assertEqual(sent, [("sendSticker", "ID1")])

    def test_decisions_mode_skips_lifecycle(self):
        table = {"allowed": "ID1", "done": "ID2", "ask_bash": "ID3"}
        sent = []
        saved = tg.api
        tg.api = lambda *a, **k: sent.append(a[1])
        try:
            tg.sticker("t", "1", "allowed", "decisions", stickers=table)
            tg.sticker("t", "1", "done", "decisions", stickers=table)
            tg.sticker("t", "1", "ask_bash", "decisions", stickers=table)
        finally:
            tg.api = saved
        self.assertEqual(len(sent), 1, "в режиме decisions уходят только итоги")

    def test_network_error_does_not_raise(self):
        saved = tg.api

        def boom(*a, **k):
            raise tg.TgFail(500, "упало")

        tg.api = boom
        try:
            self.assertFalse(
                tg.sticker("t", "1", "allowed", "all", stickers={"allowed": "ID"}))
        finally:
            tg.api = saved

    def test_every_event_has_a_kind(self):
        for name, kind in tg.STICKER_EVENTS:
            self.assertIn(kind, ("ask", "decision", "life"), name)
        self.assertEqual(len(tg.STICKER_EVENTS), len(set(tg.STICKER_KINDS)))

    def test_shipped_template_lists_all_events(self):
        shipped = json.loads(
            (ROOT / "stickers.example.json").read_text(encoding="utf-8"))
        for name, _kind in tg.STICKER_EVENTS:
            self.assertIn(name, shipped, name)

    def test_request_sticker_choice(self):
        self.assertEqual(ap.sticker_for_request("Bash", {"command": "rm -rf x"}),
                         "ask_danger")
        # curl в списке опасных, поэтому «осторожно», а не «сеть»
        self.assertEqual(ap.sticker_for_request("Bash", {"command": "curl http://x"}),
                         "ask_danger")
        self.assertEqual(ap.sticker_for_request("Bash", {"command": "git push"}),
                         "ask_bash")
        self.assertEqual(ap.sticker_for_request("Edit", {}), "ask_edit")
        self.assertEqual(ap.sticker_for_request("Write", {}), "ask_write")
        self.assertEqual(ap.sticker_for_request("WebFetch", {}), "ask_net")
        self.assertEqual(ap.sticker_for_request("Task", {}), "ask_other")


# --------------------------------------------------------------------------- #
# Контракт с Claude Code: ровно тот JSON, который он ждёт
# --------------------------------------------------------------------------- #

class ContractTest(unittest.TestCase):
    def capture(self, fn, *args, **kwargs):
        saved = sys.stdout
        sys.stdout = io.StringIO()
        try:
            fn(*args, **kwargs)
            return sys.stdout.getvalue()
        finally:
            sys.stdout = saved

    def test_allow(self):
        out = json.loads(self.capture(ap.emit, "allow"))
        self.assertEqual(out["hookSpecificOutput"]["hookEventName"],
                         "PermissionRequest")
        self.assertEqual(out["hookSpecificOutput"]["decision"],
                         {"behavior": "allow"})

    def test_deny_with_message(self):
        out = json.loads(self.capture(ap.emit, "deny", message="нельзя"))
        decision = out["hookSpecificOutput"]["decision"]
        self.assertEqual(decision["behavior"], "deny")
        self.assertEqual(decision["message"], "нельзя")

    def test_allow_with_rule(self):
        out = json.loads(self.capture(ap.emit, "allow", rule=("Bash", "git push *")))
        rule = out["hookSpecificOutput"]["decision"]["addPermissionRule"]
        self.assertEqual(rule, {"tool": "Bash", "rule": "git push *",
                                "mode": "allow"})

    def test_output_is_pure_ascii(self):
        """Иначе вывод ломается в консолях с не-UTF8 кодировкой."""
        out = self.capture(ap.emit, "deny", message="Запрещено из Telegram")
        out.encode("ascii")
        self.assertNotIn("З", out)

    def test_stop_hook_continue_shape(self):
        out = json.loads(self.capture(nt.emit_continue, "сделай ещё раз"))
        self.assertEqual(out["decision"], "block")
        self.assertEqual(out["reason"], "сделай ещё раз")
        self.assertIn("systemMessage", out)

    def test_keyboard_callback_data_fits_telegram_limit(self):
        rule = ("Bash", "git push *")
        for with_session in (True, False):
            rows = ap.keyboard_for("abcd1234", rule, with_session)
            for row in rows:
                for button in row:
                    self.assertLessEqual(
                        len(button["callback_data"].encode("utf-8")), 64)
                    self.assertLessEqual(
                        len(button["text"].encode("utf-8")), 128)

    def test_keyboard_without_rule_and_session(self):
        rows = ap.keyboard_for("abcd1234", None, False)
        actions = [b["callback_data"].split(":")[0] for row in rows for b in row]
        self.assertEqual(sorted(actions), ["a", "d", "x"])

    def test_keyboard_with_everything(self):
        rows = ap.keyboard_for("abcd1234", ("Bash", "git *"), True)
        actions = [b["callback_data"].split(":")[0] for row in rows for b in row]
        self.assertEqual(sorted(actions), ["a", "d", "r", "s", "x"])

    def test_confirm_keyboard_has_cancel(self):
        actions = [b["callback_data"].split(":")[0]
                   for row in ap.confirm_keyboard("abcd1234", "git *") for b in row]
        self.assertEqual(sorted(actions), ["R", "c"])


# --------------------------------------------------------------------------- #
# Сообщение о запросе
# --------------------------------------------------------------------------- #

class MessageTest(unittest.TestCase):
    def payload(self, **kwargs):
        base = {"cwd": r"C:\proj\моя папка", "tool_name": "Bash",
                "tool_input": {"command": "git push"}, "session_id": "abcdef12"}
        base.update(kwargs)
        return base

    def test_header_has_project_request_and_session(self):
        text = ap.build_text(self.payload(), "deadbeef")
        self.assertIn("моя папка", text)
        self.assertIn("#dead", text, "номер запроса нужен, чтобы отличать похожие")
        self.assertIn("abcd", text)

    def test_dangerous_html_is_escaped(self):
        text = ap.build_text(self.payload(
            tool_input={"command": "echo <script>alert(1)</script> & 5 < 7"}),
            "abcd1234")
        self.assertNotIn("<script>", text)
        self.assertIn("&lt;script&gt;", text)

    def test_edit_shows_before_and_after(self):
        text = ap.build_text(self.payload(
            tool_name="Edit",
            tool_input={"file_path": "a.py", "old_string": "было",
                        "new_string": "стало"}), "abcd1234")
        self.assertIn("было", text)
        self.assertIn("стало", text)

    def test_write_shows_content_head(self):
        text = ap.build_text(self.payload(
            tool_name="Write",
            tool_input={"file_path": "a.py",
                        "content": "\n".join("строка %d" % i for i in range(50))}),
            "abcd1234")
        self.assertIn("строка 0", text)
        self.assertNotIn("строка 40", text, "показываем только начало")

    def test_unknown_tool_falls_back_to_parameters(self):
        text = ap.build_text(self.payload(
            tool_name="mcp__memory__write", tool_input={"ключ": "значение"}),
            "abcd1234")
        self.assertIn("значение", text)

    def test_fits_telegram_limit(self):
        huge = "x" * 50000
        text = ap.build_text(self.payload(tool_input={"command": huge}), "abcd1234")
        self.assertLessEqual(len(text), tg.TG_LIMIT)

    def test_tool_detail_for_various_tools(self):
        self.assertEqual(ap.tool_detail("Bash", {"command": "ls"}), "ls")
        self.assertEqual(ap.tool_detail("Edit", {"file_path": "a.py"}), "a.py")
        self.assertEqual(ap.tool_detail("WebFetch", {"url": "http://x"}), "http://x")
        self.assertIn("ключ", ap.tool_detail("Other", {"ключ": 1}))


# --------------------------------------------------------------------------- #
# Уведомление о завершении
# --------------------------------------------------------------------------- #

class NotifyTest(StateCase):
    def test_prefers_last_assistant_message(self):
        path = Path(self.tmp.name) / "t.jsonl"
        path.write_text(json.dumps({
            "type": "assistant",
            "message": {"content": [{"type": "text", "text": "старое"}]}}) + "\n",
            encoding="utf-8")
        answer = nt.last_answer({"last_assistant_message": "свежее",
                                 "transcript_path": str(path)})
        self.assertEqual(answer, "свежее",
                         "расшифровка отстаёт, поле важнее")

    def test_falls_back_to_transcript(self):
        path = Path(self.tmp.name) / "t.jsonl"
        path.write_text("\n".join([
            json.dumps({"type": "user", "message": {"content": "вопрос"}}),
            json.dumps({"type": "assistant",
                        "message": {"content": [{"type": "text", "text": "первый"}]}}),
            json.dumps({"type": "assistant",
                        "message": {"content": [
                            {"type": "thinking", "thinking": "скрытое"},
                            {"type": "text", "text": "последний"}]}}),
        ]) + "\n", encoding="utf-8")
        answer = nt.last_answer({"transcript_path": str(path)})
        self.assertEqual(answer, "последний")
        self.assertNotIn("скрытое", answer)

    def test_missing_transcript_is_empty(self):
        self.assertEqual(nt.last_answer({"transcript_path": "нет.jsonl"}), "")
        self.assertEqual(nt.last_answer({}), "")

    def test_dict_shaped_message(self):
        self.assertEqual(nt.last_answer(
            {"last_assistant_message": {"content": "как словарь"}}), "как словарь")

    def test_followup_counter(self):
        self.assertEqual(nt.followup_count("s1"), 0)
        nt.set_followup_count("s1", 2)
        self.assertEqual(nt.followup_count("s1"), 2)
        nt.set_followup_count("s1", 0)
        self.assertEqual(nt.followup_count("s1"), 0)


# --------------------------------------------------------------------------- #
# Проверка при старте сессии
# --------------------------------------------------------------------------- #

class SessionTest(unittest.TestCase):
    def setUp(self):
        self.saved = tg.api
        tg.api = lambda *a, **k: {"username": "bot"}

    def tearDown(self):
        tg.api = self.saved

    def test_everything_fine_is_silent(self):
        notes = ss.problems({"TG_TOKEN": "t", "TG_CHAT_ID": "1"},
                            {"permission_mode": "default"})
        self.assertEqual(notes, [])

    def test_silent_modes_are_reported(self):
        for mode in ("auto", "bypassPermissions", "dontAsk"):
            notes = ss.problems({"TG_TOKEN": "t", "TG_CHAT_ID": "1"},
                                {"permission_mode": mode})
            self.assertTrue(any("не будут" in n for n in notes), mode)

    def test_missing_settings_reported_once(self):
        notes = ss.problems({}, {})
        self.assertEqual(len(notes), 1)
        self.assertIn("TG_TOKEN", notes[0])

    def test_missing_chat_id_hints_helper(self):
        notes = ss.problems({"TG_TOKEN": "t"}, {})
        self.assertTrue(any("get_chat_id" in n for n in notes))

    def test_unreachable_bot_reported(self):
        def boom(*a, **k):
            raise tg.TgFail(401, "Unauthorized")

        tg.api = boom
        notes = ss.problems({"TG_TOKEN": "t", "TG_CHAT_ID": "1"}, {})
        self.assertTrue(any("недоступен" in n for n in notes))

    def test_unknown_mode_is_not_a_problem(self):
        notes = ss.problems({"TG_TOKEN": "t", "TG_CHAT_ID": "1"},
                            {"permission_mode": "что-то новое"})
        self.assertEqual(notes, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
