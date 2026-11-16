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
            "FAIL_PATH": tg.FAIL_PATH,
        }
        tg.STATE_DIR = root
        tg.LOCK_PATH = root / "poll.lock"
        tg.OFFSET_PATH = root / "offset.txt"
        tg.INBOX_PATH = root / "text-inbox.json"
        tg.LOG_PATH = root / "decisions.jsonl"
        tg.STICKERS_PATH = root / "stickers.json"
        tg.FAIL_PATH = root / "poll-fail.txt"

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
        self.tmp.write("# комментарий\ngit status\ngit branch\ngit diff\nls\n"
                       "dir\npython -V\nnpm test *\n\n")
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

    def test_harmless_flags_pass(self):
        self.assertTrue(self.allowed("git branch -a"))
        self.assertTrue(self.allowed("git diff --stat"))
        self.assertTrue(self.allowed("dir /b"))

    def test_bypasses_are_refused(self):
        """То, что раньше проходило молча."""
        for command in ("git branch -D main", "git branch новая",
                        "git diff --output=C:/x.txt",
                        "git diff --no-index /dev/null ~/.ssh/id_rsa",
                        "git branch --delete main", "git status -C ../other",
                        "ls ~/.ssh"):
            self.assertFalse(self.allowed(command), command)
        for command in ("ls (Remove-Item important.txt)",
                        "dir {Remove-Item x}.Invoke()",
                        "dir $env:USERPROFILE", "dir %USERPROFILE%"):
            self.assertFalse(self.allowed(command, tool="PowerShell"), command)

    def test_wildcard_line_allows_arguments(self):
        self.assertTrue(self.allowed("npm test -- --watch src/app"))
        self.assertFalse(self.allowed("npm test; rm x"), "цепочка всё равно запрещена")

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
    def rule(self, command, tool="Bash"):
        return ap.derive_rule(tool, {"command": command})

    def test_two_words(self):
        self.assertEqual(self.rule("git push origin main"), ("Bash", "git push *"))
        self.assertEqual(self.rule("npm run build"), ("Bash", "npm run *"))

    def test_one_word_when_second_is_flag_or_path(self):
        self.assertEqual(self.rule("pytest -q"), ("Bash", "pytest *"))
        self.assertEqual(self.rule("make build.ninja"), ("Bash", "make *"))

    def test_dangerous_commands_get_no_rule(self):
        for command in ("rm -rf dist", "del x", "mv a b", "curl http://x",
                        "shutdown /s", "chmod 777 f", "RM.EXE x"):
            self.assertIsNone(self.rule(command), command)

    def test_interpreters_get_no_rule(self):
        """«python *» разрешило бы навсегда любой код."""
        for command in ("python x.py", "python3.12 x.py", "bash deploy.sh",
                        "node app.js", "npx something", "docker run img",
                        "powershell -c whoami"):
            self.assertIsNone(self.rule(command), command)
        for command in ("Remove-Item -Recurse build", "Start-Process x"):
            self.assertIsNone(self.rule(command, tool="PowerShell"), command)

    def test_risky_subcommands_get_no_rule(self):
        for command in ("git clean -fdx", "git reset --hard", "git rm x",
                        "npm exec foo", "git -c x=y status"):
            self.assertIsNone(self.rule(command), command)

    def test_chaining_and_odd_heads(self):
        self.assertIsNone(self.rule("git status && rm x"))
        self.assertIsNone(self.rule("./gradlew assembleDebug"))
        self.assertIsNone(self.rule("Invoke-Expression $x", tool="PowerShell"))
        self.assertIsNone(self.rule(""))

    def test_file_and_unknown_tools_get_no_derived_rule(self):
        self.assertIsNone(ap.derive_rule("Edit", {"file_path": "x"}))
        self.assertIsNone(ap.derive_rule("Task", {}))


class PermissionUpdateTest(unittest.TestCase):
    """Кнопки «Всегда» и «До конца сессии» — через updatedPermissions."""

    def suggest(self, *rules, **extra):
        entry = {"type": "addRules", "behavior": "allow",
                 "destination": "localSettings", "rules": list(rules)}
        entry.update(extra)
        return {"permission_suggestions": [entry]}

    def test_always_goes_to_project_settings(self):
        label, update = ap.always_update({}, "Bash", {"command": "git push origin main"})
        self.assertEqual(label, "git push *")
        self.assertEqual(update["type"], "addRules")
        self.assertEqual(update["destination"], "localSettings")
        self.assertEqual(update["rules"], [{"toolName": "Bash",
                                            "ruleContent": "git push *"}])

    def test_always_prefers_claude_suggestion(self):
        payload = self.suggest({"toolName": "Bash", "ruleContent": "npm test"},
                               destination="userSettings")
        label, update = ap.always_update(payload, "Bash", {"command": "npm test"})
        self.assertEqual(label, "npm test")
        self.assertEqual(update["destination"], "localSettings",
                         "навсегда — только в настройки проекта, не глобально")

    def test_dangerous_suggestion_is_rejected(self):
        for rule in ({"toolName": "Bash", "ruleContent": "rm -rf node_modules"},
                     {"toolName": "Bash"},
                     {"toolName": "Bash", "ruleContent": "git *"},
                     {"toolName": "Bash", "ruleContent": "python:*"},
                     {"toolName": "Edit"}):
            got = ap.always_update(self.suggest(rule), "Bash", {"command": "ls"})
            label = got[0] if got else None
            self.assertNotEqual(label, ap.label_of(rule), rule)

    def test_other_suggestion_types_ignored(self):
        payload = {"permission_suggestions": [
            {"type": "setMode", "mode": "bypassPermissions", "destination": "session"}]}
        self.assertIsNone(ap.always_update(payload, "Task", {}))
        self.assertIsNone(ap.session_update(payload, "Task", {}))

    def test_session_rule_is_exact_command(self):
        update = ap.session_update({}, "Bash", {"command": "rm -rf build"})
        self.assertEqual(update["destination"], "session")
        self.assertEqual(update["rules"][0]["ruleContent"], "rm -rf build")

    def test_session_rule_refuses_wildcards(self):
        self.assertIsNone(ap.session_update({}, "Bash", {"command": "ls *.py"}))

    def test_session_rule_for_files_needs_suggestion(self):
        self.assertIsNone(ap.session_update({}, "Edit", {"file_path": "a.py"}))
        payload = self.suggest({"toolName": "Edit", "ruleContent": "a.py"})
        self.assertEqual(ap.session_update(payload, "Edit", {})["destination"],
                         "session")


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

    def test_hook_timeout_read_from_settings(self):
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False,
                                         encoding="utf-8") as fh:
            json.dump({"hooks": {
                "Stop": [{"hooks": [{"type": "command", "timeout": 240}]}],
                "PermissionRequest": [{"matcher": ".*", "hooks": [
                    {"type": "command", "timeout": 360},
                    {"type": "command", "timeout": 90}]}],
                "SessionStart": [{"hooks": [{"type": "command"}]}],
            }}, fh)
            path = fh.name
        try:
            self.assertEqual(tg.hook_timeout("Stop", path), 240)
            # из нескольких хуков на событии важен самый строгий лимит
            self.assertEqual(tg.hook_timeout("PermissionRequest", path), 90)
            self.assertIsNone(tg.hook_timeout("SessionStart", path))
            self.assertIsNone(tg.hook_timeout("НетТакого", path))
        finally:
            os.unlink(path)

    def test_hook_timeout_survives_broken_settings(self):
        self.assertIsNone(tg.hook_timeout("Stop", "нет-такого-файла.json"))

    def test_master_switch(self):
        full = {"TG_TOKEN": "t", "TG_CHAT_ID": "1"}
        self.assertTrue(tg.enabled(full))
        self.assertTrue(tg.enabled(dict(full, CC_TG_ENABLED="1")))
        for off in ("0", "off", "no", "false"):
            self.assertFalse(tg.enabled(dict(full, CC_TG_ENABLED=off)), off)
        self.assertFalse(tg.enabled({"TG_TOKEN": "t"}), "без chat_id — выключено")
        self.assertFalse(tg.enabled({}), "без настроек — выключено")

    def test_switch_writes_env(self):
        """Переключатель правит ровно одну строку и не трогает остальное."""
        import tempfile as tf
        with tf.NamedTemporaryFile("w", suffix=".env", delete=False,
                                   encoding="utf-8") as fh:
            fh.write("TG_TOKEN=abc\nCC_TG_ENABLED=1\nCC_TG_QUIET=23:00-08:00\n")
            path = fh.name
        saved = tg.ENV_PATH
        tg.ENV_PATH = Path(path)
        try:
            self.assertTrue(tg.set_enabled(False))
            body = Path(path).read_text(encoding="utf-8")
            self.assertIn("CC_TG_ENABLED=0", body)
            self.assertIn("TG_TOKEN=abc", body)
            self.assertIn("CC_TG_QUIET=23:00-08:00", body)
            tg.set_enabled(True)
            self.assertIn("CC_TG_ENABLED=1",
                          Path(path).read_text(encoding="utf-8"))
        finally:
            tg.ENV_PATH = saved
            os.unlink(path)

    def test_switch_appends_when_key_missing(self):
        import tempfile as tf
        with tf.NamedTemporaryFile("w", suffix=".env", delete=False,
                                   encoding="utf-8") as fh:
            fh.write("TG_TOKEN=abc\n")
            path = fh.name
        saved = tg.ENV_PATH
        tg.ENV_PATH = Path(path)
        try:
            tg.set_enabled(False)
            self.assertIn("CC_TG_ENABLED=0",
                          Path(path).read_text(encoding="utf-8"))
        finally:
            tg.ENV_PATH = saved
            os.unlink(path)

    def test_commands_recognised(self):
        sent, saved_send, saved_env = [], tg.send, tg.ENV_PATH
        # Журнал уводим во временный файл: тесты не должны писать в настоящий.
        saved_log = tg.LOG_PATH
        tg.LOG_PATH = Path(tempfile.gettempdir()) / "cc-tg-test-log.jsonl"
        import tempfile as tf
        with tf.NamedTemporaryFile("w", suffix=".env", delete=False,
                                   encoding="utf-8") as fh:
            fh.write("TG_TOKEN=t\nTG_CHAT_ID=1\nCC_TG_ENABLED=1\n")
            path = fh.name
        tg.ENV_PATH = Path(path)
        tg.send = lambda token, chat, text, **k: sent.append(text)
        try:
            self.assertTrue(tg.handle_command("/off", "t", "1"))
            self.assertIn("CC_TG_ENABLED=0",
                          Path(path).read_text(encoding="utf-8"))
            self.assertTrue(tg.handle_command("/on", "t", "1"))
            self.assertIn("CC_TG_ENABLED=1",
                          Path(path).read_text(encoding="utf-8"))
            self.assertTrue(tg.handle_command("/status", "t", "1"))
            self.assertTrue(tg.handle_command("/help", "t", "1"))
            self.assertFalse(tg.handle_command("обычный текст", "t", "1"),
                             "обычный текст командой не считается")
            self.assertFalse(tg.handle_command("/etc/hosts поправь", "t", "1"),
                             "инструкция с косой чертой — не команда")
            self.assertFalse(tg.handle_command("/чтото", "t", "1"))
            self.assertEqual(len(sent), 4, "на каждую команду — один ответ")
        finally:
            tg.send, tg.ENV_PATH, tg.LOG_PATH = saved_send, saved_env, saved_log
            os.unlink(path)

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

    def test_text_uses_sent_time_not_received(self):
        """Иначе сообщение, написанное до начала ожидания, подхватится как ответ."""
        long_ago = time.time() - 3600
        parsed = tg.parse_text(
            {"from": {"id": "111"}, "text": "старое", "date": long_ago}, "111")
        self.assertAlmostEqual(parsed["ts"], long_ago, delta=1)
        self.assertLess(parsed["ts"], time.time() - 60)

    def test_text_without_date_falls_back_to_now(self):
        parsed = tg.parse_text({"from": {"id": "111"}, "text": "без даты"}, "111")
        self.assertAlmostEqual(parsed["ts"], time.time(), delta=5)

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
        old = time.time() - tg.LOCK_SUSPECT - 5
        os.utime(str(tg.LOCK_PATH), (old, old))
        self.assertFalse(tg.take_lock(), "сначала только снимаем чужой флажок")
        self.assertTrue(tg.take_lock(), "после снятия флажок свободен")
        tg.drop_lock()

    def test_fresh_lock_is_not_checked_for_liveness(self):
        """Проверка живости дорогая — свежий флажок её не вызывает."""
        tg.LOCK_PATH.write_text("999999999", encoding="ascii")
        calls, saved = [], tg.pid_alive
        tg.pid_alive = lambda pid: calls.append(pid) or False
        try:
            self.assertFalse(tg.take_lock())
        finally:
            tg.pid_alive = saved
        self.assertEqual(calls, [])
        self.assertTrue(tg.LOCK_PATH.exists())

    def test_foreign_lock_is_not_dropped(self):
        tg.LOCK_PATH.write_text("424242", encoding="ascii")
        tg.drop_lock()
        self.assertTrue(tg.LOCK_PATH.exists(), "чужой флажок трогать нельзя")

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

    def test_inbox_takes_only_reply_when_waiting_for_one(self):
        now = time.time()
        tg.inbox_put({"ts": now, "text": "общее", "reply_to": None})
        tg.inbox_put({"ts": now, "text": "адресное", "reply_to": 7})
        self.assertEqual(tg.inbox_take(now - 1, reply_to=7)["text"], "адресное")
        self.assertIsNone(tg.inbox_take(now - 1, reply_to=7),
                          "простой текст без привязки ответом не считается")

    def test_reply_for_other_session_is_not_taken(self):
        """Две сессии ждут: ответ сессии A не должен достаться B."""
        now = time.time()
        tg.inbox_put({"ts": now, "text": "для A", "reply_to": 100})
        self.assertIsNone(tg.inbox_take(now - 1, reply_to=200))
        self.assertEqual(tg.inbox_take(now - 1, reply_to=100)["text"], "для A")

    def test_pending_roundtrip(self):
        tg.pending_put("abcd1234", 111, 7, "текст вопроса")
        path = tg.STATE_DIR / "pending-abcd1234.json"
        self.assertTrue(path.exists())
        tg.pending_drop("abcd1234")
        self.assertFalse(path.exists())

    def test_sweep_clears_abandoned_question(self):
        """Ответили в терминале, процесс убит — кнопки должны исчезнуть."""
        tg.pending_put("dead0001", 111, 7, "вопрос")
        data = json.loads((tg.STATE_DIR / "pending-dead0001.json").read_text("utf-8"))
        data["pid"] = 999999999                 # такого процесса нет
        (tg.STATE_DIR / "pending-dead0001.json").write_text(
            json.dumps(data), encoding="utf-8")

        edits = []
        saved = tg.api
        tg.api = lambda token, method, payload, timeout=30: edits.append(
            (method, payload.get("message_id"), payload.get("text")))
        try:
            tg.sweep_pending("tok")
        finally:
            tg.api = saved
        self.assertEqual(len(edits), 1, "сообщение должны переписать один раз")
        self.assertEqual(edits[0][0], "editMessageText")
        self.assertIn("терминале", edits[0][2])
        self.assertFalse((tg.STATE_DIR / "pending-dead0001.json").exists())

    def test_sweep_spares_live_question(self):
        """Пока процесс жив и ждёт, трогать его сообщение нельзя."""
        tg.pending_put("alive001", 111, 7, "вопрос")     # pid текущий
        edits = []
        saved = tg.api
        tg.api = lambda *a, **k: edits.append(a)
        try:
            tg.sweep_pending("tok")
        finally:
            tg.api = saved
        self.assertEqual(edits, [])
        self.assertTrue((tg.STATE_DIR / "pending-alive001.json").exists())

    def test_sweep_survives_broken_file(self):
        (tg.STATE_DIR / "pending-broken.json").write_text("{не json", encoding="utf-8")
        tg.sweep_pending("tok")
        self.assertFalse((tg.STATE_DIR / "pending-broken.json").exists())

    def press_update(self, rid):
        return {"update_id": 5, "callback_query": {
            "id": "cb", "from": {"id": "111"}, "data": "a:" + rid,
            "message": {"message_id": 7, "chat": {"id": 111}}}}

    def run_wait(self, updates, rid):
        """Один проход опроса с подставным Telegram."""
        calls = []
        saved_api, saved_answer = tg.api, tg.answer_press
        box = list(updates)

        def fake_api(token, method, payload, timeout=30):
            calls.append(method)
            if method == "getUpdates":
                out, box[:] = list(box), []
                return out
            return {}

        tg.api = fake_api
        tg.answer_press = lambda *a, **k: calls.append("answer")
        try:
            return tg.wait("t", "111", time.time() + 1.5, rid=rid), calls
        finally:
            tg.api, tg.answer_press = saved_api, saved_answer

    def test_orphan_press_is_closed(self):
        """Нажатие, которого никто не ждёт, нужно закрыть, а не копить."""
        (kind, _data), calls = self.run_wait(
            [self.press_update("ffff0000")], rid="aaaa1111")
        self.assertIsNone(kind)
        self.assertIn("editMessageReplyMarkup", calls)
        self.assertFalse((tg.STATE_DIR / "cb-ffff0000.json").exists(),
                         "осиротевшее нажатие не кладём на полочку")

    def test_press_for_live_question_goes_to_mailbox(self):
        """А вот нажатие для живого запроса обязано дойти до адресата."""
        tg.pending_put("ffff0000", 111, 7, "вопрос")
        (kind, _data), calls = self.run_wait(
            [self.press_update("ffff0000")], rid="aaaa1111")
        self.assertIsNone(kind)
        self.assertNotIn("editMessageReplyMarkup", calls)
        self.assertTrue((tg.STATE_DIR / "cb-ffff0000.json").exists())

    def test_state_file_cannot_escape_folder(self):
        path = tg.state_file("followup", "../../злой")
        self.assertEqual(path.parent, tg.STATE_DIR)

    def test_state_dir_is_not_shared_temp(self):
        """Файл с нажатием равносилен нажатию — общей временной папке не место."""
        shipped = Path(self.saved["STATE_DIR"])
        temp = Path(os.environ.get("TEMP") or os.environ.get("TMP") or "/tmp")
        if not os.environ.get("CC_TG_STATE_DIR"):
            self.assertNotEqual(shipped.parent, temp)
            self.assertIn(".claude", shipped.parts)

    def test_wait_does_not_spin_on_empty_queue(self):
        """Без offset.txt и с пустой очередью — не тысячи запросов в секунду."""
        calls, saved = [], tg.api
        tg.api = lambda token, method, payload, timeout=30: calls.append(
            payload.get("timeout")) or []
        try:
            tg.wait("t", "1", time.time() + 1.5, rid="aaaa1111")
        finally:
            tg.api = saved
        self.assertLess(len(calls), 10, "запросов: %d" % len(calls))
        self.assertEqual(tg.read_offset(), 0, "после пустого ответа — обычный опрос")

    def test_wait_takes_only_reply_and_hints_once(self):
        """Текст без «Ответить» не принимается, а человеку подсказывают как."""
        sent, saved_api, saved_send = [], tg.api, tg.send
        box = [[{"update_id": 9, "message": {
            "message_id": 50, "from": {"id": "1"}, "date": time.time() + 1,
            "text": "просто текст"}}]]

        def fake(token, method, payload, timeout=30):
            return box.pop() if box else []

        tg.api = fake
        tg.send = lambda token, chat, text, **k: sent.append(text)
        tg.write_offset(1)
        try:
            kind, _ = tg.wait("t", "1", time.time() + 1.5, want_text=True,
                              reply_to=777)
        finally:
            tg.api, tg.send = saved_api, saved_send
        self.assertIsNone(kind)
        self.assertEqual(len(sent), 1)
        self.assertIn("Ответить", sent[0])

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
        self.assertEqual(ap.sticker_for_request(
            "PowerShell", {"command": "Remove-Item -Recurse x"}), "ask_danger")


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

    def test_allow_with_permission_update(self):
        """Формат из документации хуков: updatedPermissions, не addPermissionRule."""
        update = {"type": "addRules", "behavior": "allow",
                  "destination": "localSettings",
                  "rules": [{"toolName": "Bash", "ruleContent": "git push *"}]}
        out = json.loads(self.capture(ap.emit, "allow", updates=update))
        decision = out["hookSpecificOutput"]["decision"]
        self.assertNotIn("addPermissionRule", decision)
        self.assertEqual(decision["updatedPermissions"], [update])

    def test_decision_survives_failed_message_edit(self):
        """Сбой правки сообщения раньше съедал уже принятое решение."""
        def fake(token, method, payload, timeout=30):
            if method == "editMessageText":
                raise tg.TgFail(400, "message is not modified")
            return {}

        ask = ap.Ask(token="t", chat_id="1", chat=1, msg=5, rid="aaaa1111",
                     text="вопрос", base={}, detail="d", started=time.time(),
                     always=None, session_rule=None)
        saved_api, saved_log = tg.api, tg.log
        tg.api, tg.log = fake, (lambda record: None)
        try:
            out = self.capture(ap.finish, ask, "allow", "✅", "allowed", "telegram",
                               press={"callback_id": "c", "chat_id": 1,
                                      "message_id": 5})
        finally:
            tg.api, tg.log = saved_api, saved_log
        self.assertEqual(json.loads(out)["hookSpecificOutput"]["decision"],
                         {"behavior": "allow"})

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
        rule = "git push *"
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
        rows = ap.keyboard_for("abcd1234", "git push *", True)
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

    def test_big_edit_stays_valid_html(self):
        """Раньше обрезка резала внутри <pre>, и Telegram отвергал вопрос."""
        line = ("значение = вызов_функции(аргумент_один, аргумент_два) "
                "# длинный комментарий к строке кода ")
        big = "\n".join(line + str(i) for i in range(8))
        text = ap.build_text(self.payload(
            tool_name="MultiEdit",
            tool_input={"file_path": "a.py",
                        "edits": [{"old_string": big, "new_string": big}] * 3}),
            "abcd1234")
        self.assertLessEqual(len(text), tg.CHUNK)
        self.assertEqual(text.count("<pre>"), text.count("</pre>"))
        self.assertIn("a.py", text, "главное — путь — остаётся всегда")
        self.assertIn("сокращено", text)

    def test_secrets_are_masked_in_command(self):
        text = ap.build_text(self.payload(tool_input={
            "command": "curl -H 'Authorization: Bearer abcdefghijklmnop123' "
                       "https://x?api_key=SECRETVALUE123"}), "abcd1234")
        self.assertNotIn("abcdefghijklmnop123", text)
        self.assertNotIn("SECRETVALUE123", text)

    def test_env_file_content_is_hidden(self):
        text = ap.build_text(self.payload(
            tool_name="Write",
            tool_input={"file_path": r"C:\proj\.env",
                        "content": "DB_HOST=localhost\nSOMETHING=value"}),
            "abcd1234")
        self.assertNotIn("localhost", text)
        self.assertIn("скрыто", text)

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


# --------------------------------------------------------------------------- #
# Исправленные баги и уязвимости разбора от 16.11.2026
# --------------------------------------------------------------------------- #

class MarkupFixTest(unittest.TestCase):
    def test_chunks_keep_every_tag_balanced(self):
        """Раньше рвался <b>: часть сообщения не уходила вовсе."""
        md = "Начало **" + ("жирный текст " * 400) + "** конец *курсив* `код`"
        for part in tg.chunks(tg.md_to_html(md), 1500):
            for name in ("b", "pre", "code", "i"):
                self.assertEqual(part.count("<%s>" % name),
                                 part.count("</%s>" % name), (name, part[:60]))

    def test_chunks_do_not_split_entity(self):
        parts = tg.chunks("x" * 3298 + "&amp;" + "y" * 400, 3500)
        for part in parts:
            self.assertNotRegex(part, r"&[a-z]{0,4}$", "сущность разрезана")
        self.assertIn("&amp;", "".join(parts))

    def test_link_with_quote_is_valid(self):
        out = tg.md_to_html('[x](https://e.com/a"b)')
        self.assertIn('href="https://e.com/a&quot;b"', out)

    def test_plain_strips_markup(self):
        self.assertEqual(tg.plain("<b>a &amp; b</b>"), "a & b")

    def test_send_falls_back_to_plain_text(self):
        """Если Telegram не принял разметку — шлём простым текстом, а не теряем."""
        calls, saved = [], tg.api

        def fake(token, method, payload, timeout=30):
            calls.append(dict(payload))
            if payload.get("parse_mode"):
                raise tg.TgFail(400, "can't parse entities")
            return {"message_id": 1}

        tg.api = fake
        try:
            tg.send("t", "1", "<b>сломанная <i>разметка</b>")
        finally:
            tg.api = saved
        self.assertEqual(len(calls), 2)
        self.assertNotIn("parse_mode", calls[1])
        self.assertEqual(calls[1]["text"], "сломанная разметка")

    def test_edit_never_raises(self):
        saved = tg.api

        def boom(*a, **k):
            raise tg.TgFail(500, "упало")

        tg.api = boom
        try:
            self.assertFalse(tg.edit("t", "1", 5, "текст"))
            self.assertFalse(tg.edit_keyboard("t", "1", 5, []))
        finally:
            tg.api = saved


class SecretTest(StateCase):
    def test_known_token_formats(self):
        for secret in ("8928902244:AAEXieh1VSHl8sFdB0V_6orFWd9TmAATyl8",
                       "ghp_" + "a" * 36, "sk-" + "b" * 32, "AKIA" + "C" * 16):
            self.assertNotIn(secret, tg.redact("ключ: %s конец" % secret), secret)

    def test_assignments_are_masked_but_key_kept(self):
        out = tg.redact("export API_KEY=abc123 && TG_TOKEN=xyz PASSWORD='p a s s'")
        for value in ("abc123", "xyz", "p a s s"):
            self.assertNotIn(value, out)
        self.assertIn("API_KEY=", out)

    def test_ordinary_text_untouched(self):
        text = "git push origin main и CC_TG_TIMEOUT=300"
        self.assertEqual(tg.redact(text), text)

    def test_sensitive_paths(self):
        for path in (".env", r"C:\p\.env.local", "~/.ssh/id_rsa", "cert.pem",
                     "app/secrets.yaml", "/home/u/.netrc"):
            self.assertTrue(tg.sensitive_path(path), path)
        for path in ("app.py", "README.md", "environment.py"):
            self.assertFalse(tg.sensitive_path(path), path)

    def test_log_masks_secrets(self):
        tg.log({"via": "test", "detail": "curl -H 'Authorization: Bearer "
                                          "abcdefghijklmnop123'"})
        self.assertNotIn("abcdefghijklmnop123",
                         tg.LOG_PATH.read_text(encoding="utf-8"))
        json.loads(tg.LOG_PATH.read_text(encoding="utf-8").strip())

    def test_log_rotates(self):
        saved = tg.LOG_MAX
        tg.LOG_MAX = 100
        try:
            for _ in range(10):
                tg.log({"via": "test", "detail": "x" * 30})
        finally:
            tg.LOG_MAX = saved
        self.assertTrue(Path(str(tg.LOG_PATH) + ".1").exists())
        self.assertLess(tg.LOG_PATH.stat().st_size, 400)


class NotifyFixTest(StateCase):
    def test_new_human_turn_resets_followup_budget(self):
        """После трёх продолжений функция больше не умирает до конца сессии."""
        nt.set_followup_count("s1", 3)
        self.assertEqual(nt.followup_budget({"stop_hook_active": True}, "s1", 3), 3)
        self.assertEqual(nt.followup_budget({"stop_hook_active": False}, "s1", 3), 0)
        self.assertEqual(nt.followup_count("s1"), 0)

    def test_silent_mode_warned_once_per_session(self):
        payload = {"permission_mode": "auto"}
        self.assertIn("auto", nt.mode_warning(payload, "s1"))
        self.assertEqual(nt.mode_warning(payload, "s1"), "")
        self.assertEqual(nt.mode_warning({"permission_mode": "default"}, "s2"), "")

    def test_no_network_without_settings(self):
        calls, saved = [], tg.api
        tg.api = lambda *a, **k: calls.append(a) or []
        try:
            self.assertFalse(tg.poll_commands("", ""))
        finally:
            tg.api = saved
        self.assertEqual(calls, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
