# Стикер-пак: задание на отрисовку

Маскот — чиби-персонаж, который живёт в Telegram и реагирует на то, что делает
Claude Code. Роль как у совы Duolingo: он не украшение, а индикатор — по стикеру
должно быть понятно, что произошло, даже не читая текст.

Ключ в первой колонке — имя события в `stickers.json`, его нельзя менять.
Эмоция важнее реквизита: один и тот же персонаж, 24 состояния.

## Вопросы — приходят вместе с кнопками

| Ключ | Что произошло | Поза и эмоция |
|---|---|---|
| `ask_bash` | спрашивает про команду | сидит у терминала, палец занесён над Enter, смотрит на тебя вопросительно |
| `ask_edit` | правка существующего файла | держит карандаш больше себя, занёс над страницей, бровь поднята |
| `ask_write` | создаёт новый файл | протягивает чистый лист, глаза горят |
| `ask_danger` | `rm`, `del`, `mv`, `curl`, `shutdown` | обеими руками держит красную кнопку, капля пота, глаза расширены: «ты точно уверен?» |
| `ask_net` | выход в интернет (`WebFetch`, поиск) | держит глобус на поводке, тот рвётся наружу |
| `ask_other` | всё остальное | чешет затылок, над головой знак вопроса |

## Решения — после нажатия кнопки

| Ключ | Что произошло | Поза и эмоция |
|---|---|---|
| `allowed` | нажали «Разрешить» | кулак вверх, глаза-звёздочки, полный восторг |
| `denied` | нажали «Запретить» | руки крест-накрест, строгие брови, непреклонен |
| `denied_reason` | запретили и написали причину | держит табличку с запиской, читает её вслух |
| `session_allowed` | разрешено до конца сессии | подмигивает, в руках песочные часы |
| `always_allowed` | разрешено навсегда | торжественно ставит печать «НАВСЕГДА», парадная поза |
| `timeout` | 5 минут тишины, вопрос ушёл в терминал | заснул сидя, пузырь из носа, рядом будильник |
| `safelist` | одобрено автоматически по безопасному списку | расслабленно машет рукой «проходи», даже не смотрит |

## Режимы — приходят один раз при старте сессии

| Ключ | Что происходит | Поза и эмоция |
|---|---|---|
| `mode_default` | ручной режим, вопросы приходят | стоит по стойке «смирно», готов спрашивать |
| `mode_auto` | авто: решает сам, в Telegram тихо | пропеллер на голове, глаза закрыты, делает всё сам |
| `mode_plan` | режим плана | в строительной каске, разворачивает чертёж |
| `mode_accept_edits` | правки без вопросов | с валиком красит стену, не оборачиваясь |
| `mode_bypass` | без ограничений, самый опасный | рок-жест, нимб падает с головы, искры из глаз |

## Жизненный цикл

| Ключ | Что произошло | Поза и эмоция |
|---|---|---|
| `session_start` | сессия началась, всё настроено | машет рукой, рядом чашка кофе |
| `done` | Claude закончил ответ | вытирает пот со лба, над головой галочка |
| `waiting_reply` | ждёт твой текст с телефона | держит телефон двумя руками, выжидающе смотрит |
| `followup_sent` | передал твой ответ в работу | бежит с конвертом, шарф развевается |
| `quiet_hours` | тихие часы, не беспокоит | в пижаме, палец к губам: «тссс» |
| `error` | бот недоступен, настройки сломаны | запутался в проводах, искры, глаза-кружочки |

## Технические требования Telegram

- **Статичные:** PNG или WEBP, строго 512×512, прозрачный фон, до 512 КБ.
  Одна сторона обязана быть ровно 512 пикселей.
- **Анимированные:** формат `.tgs` (Lottie через плагин Bodymovin для After
  Effects), до 64 КБ, 512×512, не длиннее 3 секунд, 60 кадров в секунду.
- Каждому стикеру присваивается эмодзи — лучше разные, так их проще искать.
- В паке до 120 стикеров, так что 24 — с запасом на будущее.
- Контур персонажа стоит делать с белой обводкой: на тёмной теме Telegram
  тёмный силуэт теряется.

## Если рисуете через ИИ

Главная сложность — чтобы на всех 24 картинках был **один и тот же** персонаж.
Поэтому: один референс во все запросы, один базовый блок описания, и меняется
только строка с позой.

**База — вставляйте в каждый запрос без изменений** (подставьте приметы своего
персонажа вместо подсказок в угловых скобках):

```
chibi anime mascot, <hair colour and style>, <eye colour>, <outfit>,
same character as the reference image, keep identical face, hair and outfit,
full body, centered, facing viewer, thick white outline, flat cel shading,
bold simple shapes that stay readable at 128 pixels,
plain solid #00FF00 background, no text, no watermark, no shadow on background,
square 1:1 composition with small margin around the character
```

**Позы — по одной строке на событие:**

| Ключ | Строка в запрос |
|---|---|
| `ask_bash` | `sitting at a retro computer terminal, index finger hovering over the Enter key, head turned to viewer, one eyebrow raised, questioning expression` |
| `ask_edit` | `holding an oversized pencil above an open page, eyebrow raised, about to write` |
| `ask_write` | `offering a blank sheet of paper with both hands, sparkling excited eyes` |
| `ask_danger` | `gripping a big red button with both hands, sweat drop on forehead, wide alarmed eyes, mouth open in warning` |
| `ask_net` | `holding a small globe on a leash that pulls away, leaning back, strained smile` |
| `ask_other` | `scratching the back of the head, large question mark floating above, puzzled` |
| `allowed` | `triumphant fist raised high, star-shaped sparkling eyes, wide happy grin` |
| `denied` | `arms crossed in a firm X in front of the chest, stern frown, narrowed eyes` |
| `denied_reason` | `holding up a small blank signboard, other hand raised explaining, serious but calm` |
| `session_allowed` | `winking with one eye, holding an hourglass, thumbs up` |
| `always_allowed` | `ceremonially pressing a large stamp onto a scroll, proud posture, eyes closed` |
| `timeout` | `asleep sitting upright, snot bubble, head tilted, alarm clock beside, zzz symbols` |
| `safelist` | `waving one hand casually to let someone pass, relaxed, eyes closed, not even looking` |
| `mode_default` | `standing at attention like a soldier, alert eyes, ready and waiting` |
| `mode_auto` | `propeller beanie spinning on the head, eyes closed, calmly working alone` |
| `mode_plan` | `wearing a construction helmet, unrolling a large blueprint` |
| `mode_accept_edits` | `painting a wall with a paint roller, seen from behind over the shoulder, humming` |
| `mode_bypass` | `throwing a rock-on hand sign, halo slipping off the head, mischievous grin, small sparks` |
| `session_start` | `waving hello with one hand, holding a coffee mug in the other, cheerful` |
| `done` | `wiping sweat off the forehead, large check mark floating above, satisfied smile` |
| `waiting_reply` | `holding a phone with both hands, looking up expectantly, hopeful eyes` |
| `followup_sent` | `running forward carrying an envelope, scarf fluttering, determined` |
| `quiet_hours` | `wearing pyjamas and a nightcap, finger pressed to the lips shushing, sleepy half-closed eyes` |
| `error` | `tangled in cables, small sparks around, swirl spiral eyes, dazed` |

**Что обычно портит результат:**

- Разные запросы без референса — персонаж «плывёт»: меняются причёска, цвет
  глаз, детали одежды. Всегда прикладывайте одну и ту же картинку как референс
  и держите базовый блок дословно одинаковым.
- Мелкие детали: на 128 пикселях (так стикер виден в чате) исчезают тонкие
  линии, мелкий реквизит и надписи. Проверяйте каждую картинку, уменьшив её.
- Текст внутри стикера ИИ почти всегда пишет с ошибками — лучше вообще без
  надписей, либо добавлять их потом вручную.

**Доведение до требований Telegram:**

1. Убрать фон. Однотонный `#00FF00` снимается в один клик почти любым
   инструментом удаления фона; если выводить сразу с прозрачностью не
   получается, так надёжнее.
2. Привести к 512×512 с прозрачностью. Пакетно, без установки библиотек, если
   есть ImageMagick:

   ```powershell
   magick mogrify -resize 512x512 -background none -gravity center -extent 512x512 *.png
   ```

3. Проверить вес: до 512 КБ на статичный стикер.
4. Собрать пак через @Stickers и разложить по событиям:
   `python get_sticker_ids.py --assign`.

## Как подключить

Самый короткий путь — без сборки пака через @Stickers. Telegram принимает
PNG 512×512 прямо в `sendSticker` и возвращает идентификатор, который мы и
запоминаем.

1. Положите лист со стикерами (прозрачный фон, стикеры не соприкасаются) в
   `stiker_pack/` и нарежьте:

   ```powershell
   python make_stickers.py stiker_pack/лист.png
   python make_stickers.py --contact stiker_pack/ready
   ```

2. Назовите файлы по событиям: `ask_bash.png`, `allowed.png`, `timeout.png` и
   так далее — имена берутся из таблиц выше.
3. Загрузите:

   ```powershell
   python get_sticker_ids.py --upload stiker_pack/ready
   ```

   Повторная загрузка отдельных стикеров: допишите их имена в конец команды.
4. Проверить карту: `python get_sticker_ids.py --list`.

Если пак уже собран в Telegram, работает и старый путь: `python
get_sticker_ids.py --assign` — скрипт по очереди просит прислать стикер для
каждого события.

Пока `stickers.json` пустой, стикеры не отправляются вообще. Как только там
появился хотя бы один идентификатор, отправка включается сама. Управление —
`CC_TG_STICKERS` в `.env`: `all` (всё), `decisions` (только итоги), `off`.
