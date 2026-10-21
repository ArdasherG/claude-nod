# Одобрение запросов Claude Code через Telegram

Когда Claude Code нужно разрешение на действие, вопрос приходит в Telegram с кнопками
«✅ Разрешить / ❌ Запретить». Отвечаете с телефона — Claude Code продолжает работу.

Если ответа нет 5 минут, или пропал интернет, или бот недоступен — скрипт молча
завершается, и вопрос задаётся обычным способом в терминале. **Claude Code не зависает
и не ломается ни при каком раскладе.**

## Что в папке

| Файл | Назначение |
|---|---|
| `tg_approve.py` | основной скрипт: вопрос в Telegram и ожидание нажатия |
| `tg_notify.py` | уведомление «Claude закончил» (необязательный хук `Stop`) |
| `get_chat_id.py` | помощник: показывает ваш `chat_id` |
| `.env` | токен бота и `chat_id`. **В git не попадает** |
| `safelist.txt` | команды, одобряемые сразу, без сообщения в телефон |
| `decisions.jsonl` | журнал всех запросов и решений (создаётся сам) |
| `tests/` | заготовки запросов для ручной проверки |

Нужен только Python 3.8+ — никаких устанавливаемых библиотек.

## Настройка

1. **Бот.** Если бота ещё нет: напишите [@BotFather](https://t.me/BotFather) → `/newbot`.
   Полученный токен впишите в `.env`, в строку `TG_TOKEN=`.
2. **chat_id.** Откройте своего бота в Telegram и нажмите «Start» (или отправьте любое
   сообщение). Затем:

   ```powershell
   python "C:\Users\Neo\Desktop\Python\projects\Cou-cou analog\get_chat_id.py"
   ```

   Скопируйте показанный номер в `.env`, в строку `TG_CHAT_ID=`.
3. **Хук.** Добавьте в `~/.claude/settings.json` (то есть
   `C:\Users\Neo\.claude\settings.json`) раздел `hooks`:

   ```json
   "hooks": {
     "PermissionRequest": [
       {
         "matcher": ".*",
         "hooks": [
           {
             "type": "command",
             "command": "C:\\Users\\Neo\\AppData\\Local\\Programs\\Python\\Python312\\python.exe",
             "args": ["C:\\Users\\Neo\\Desktop\\Python\\projects\\Cou-cou analog\\tg_approve.py"],
             "timeout": 360,
             "statusMessage": "Жду одобрения в Telegram..."
           }
         ]
       }
     ],
     "Stop": [
       {
         "hooks": [
           {
             "type": "command",
             "command": "C:\\Users\\Neo\\AppData\\Local\\Programs\\Python\\Python312\\python.exe",
             "args": ["C:\\Users\\Neo\\Desktop\\Python\\projects\\Cou-cou analog\\tg_notify.py"],
             "timeout": 15
           }
         ]
       }
     ]
   }
   ```

   Пояснения:
   - Путь к Python указан полностью. Не `python` из PATH и **не**
     `WindowsApps\python3` — последнее заглушка от Microsoft Store.
   - В JSON обратные слэши удваиваются (`\\`).
   - Программа (`command`) и путь к скрипту (`args`) разделены, потому что в пути есть
     пробел — так не нужно возиться с кавычками.
   - `timeout: 360` — сколько секунд Claude Code готов ждать скрипт. Больше, чем наши
     300 секунд ожидания, поэтому обрыв по этому лимиту не случится первым.
   - `matcher: ".*"` — на все инструменты. Чтобы сообщений было меньше, можно сузить до
     `"Bash|PowerShell|Edit|Write"`.
   - Раздел `Stop` необязателен — это уведомление «Claude закончил».
4. Перезапустите Claude Code.

## Что приходит в Telegram

```
🔔 Cou-cou analog
🔧 Bash
    git push origin main
💬 Отправить коммиты на сервер
♾ «Всегда» добавит правило: git push *
📁 C:\Users\Neo\Desktop\Python\projects\Cou-cou analog

[ ✅ Разрешить ]  [ ❌ Запретить ]
[ ♾ Всегда: git push * ]
```

Третья кнопка добавляет постоянное правило, чтобы похожие запросы больше не приходили.
Опасным командам (`rm`, `del`, `mv`, `curl`, `shutdown`…) эта кнопка не предлагается:
одно случайное нажатие разрешило бы их навсегда.

Для `Edit` и `Write` в сообщении видно, что именно меняется — «было / станет» и первые
строки нового содержимого.

После нажатия сообщение превращается в «✅ Разрешено в 11:42», кнопки исчезают.

## Безопасный список

Команды из `safelist.txt` одобряются сразу на компьютере, без сообщения в телефон —
чтобы `git status` не присылал уведомление сто раз в день. Правила сравнения:

- совпадение по началу команды (`git status` покрывает `git status --short`);
- если в команде есть `&&`, `;`, `|`, `` ` ``, `>`, `<` — список **не** применяется,
  чтобы `git status && rm -rf x` не проскочил;
- `cat`, `head`, `tail` намеренно отсутствуют: иначе чтение любого файла, включая `.env`
  и ключи, одобрялось бы молча.

Список действует только при заполненном `.env`. Если токен не настроен, скрипт вообще
ничего не решает — все вопросы идут в терминал.

## Проверка руками

```powershell
cd "C:\Users\Neo\Desktop\Python\projects\Cou-cou analog"

# 1. Обычный путь: придёт сообщение в Telegram, нажмите «Разрешить»
Get-Content tests\payload_bash.json -Raw | python tg_approve.py
# ожидаем на выходе:
# {"hookSpecificOutput": {"hookEventName": "PermissionRequest", "decision": {"behavior": "allow"}}}

# 2. Запрет (нажмите «Запретить»)
Get-Content tests\payload_edit.json -Raw | python tg_approve.py

# 3. Безопасный список: разрешение мгновенно, в Telegram ничего не приходит
Get-Content tests\payload_safe.json -Raw | python tg_approve.py

# 4. Таймаут, укороченный до 15 секунд — кнопку не нажимать
$env:CC_TG_TIMEOUT=15
Get-Content tests\payload_bash.json -Raw | python tg_approve.py
$env:CC_TG_TIMEOUT=$null
# ожидаем: на выходе пусто, в Telegram «⏳ Время истекло»

# 5. Неверный токен / нет сети: тоже пусто и быстрый выход
$env:TG_TOKEN="123:bad"
Get-Content tests\payload_bash.json -Raw | python tg_approve.py
$env:TG_TOKEN=$null

# 6. Мусор на входе
"" | python tg_approve.py
"not json" | python tg_approve.py

# 7. Два запроса одновременно: придут два сообщения,
#    нажмите в обратном порядке — каждый получит своё решение
Start-Job { Set-Location $using:PWD; Get-Content tests\payload_bash.json -Raw | python tg_approve.py }
Start-Job { Set-Location $using:PWD; Get-Content tests\payload_edit.json -Raw | python tg_approve.py }
Get-Job | Wait-Job | Receive-Job
```

После каждого запуска `$LASTEXITCODE` должен быть `0`.

## Если что-то не работает

1. **Журнал.** `decisions.jsonl` — по строке на каждый запрос, с причиной
   (`safelist`, `telegram`, `timeout`, `send_failed`, `error`).
2. **Подробные сообщения.** Поставьте в `.env` `CC_TG_DEBUG=1` — скрипт начнёт писать,
   что происходит, в поток ошибок. На решения это не влияет.
3. **Хук не запускается.** Запустите `claude --debug` — видно, какие хуки вызывались и
   что они напечатали. Проверьте, что в `settings.json` корректный JSON и путь к Python
   существует.
4. **Сообщения не приходят.** Проверьте токен: откройте в браузере
   `https://api.telegram.org/bot<ТОКЕН>/getMe`. И что вы нажали «Start» у бота —
   без этого бот не имеет права вам писать.

## Одновременные запросы

Claude иногда вызывает несколько инструментов за раз, и скрипт запускается в нескольких
копиях. Telegram отдаёт нажатия только одному слушателю, поэтому копии договариваются
через файлы в `%TEMP%\cc-tg-approve\`: кто первый создал `poll.lock`, тот и спрашивает
Telegram, а чужие нажатия раскладывает в файлы `cb-<номер>.json` — остальные забирают
оттуда своё. Ничего не теряется. Если копию скрипта убили и `poll.lock` остался, он
снимается автоматически через минуту.

## Безопасность

- `.env` в `.gitignore` — токен не попадёт в репозиторий.
- Нажатия принимаются **только** от вашего `chat_id`. Посторонний получит «Нет доступа».
- Проверяется и номер запроса, так что старое сообщение в истории чата не может повлиять
  на новый запрос.
- Текущий токен был прислан в переписке открытым текстом. Стоит перевыпустить его:
  `/revoke` у @BotFather, затем обновить `.env`.
