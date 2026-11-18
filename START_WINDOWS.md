# Быстрый старт на Windows

Все команды выполняйте в терминале, открытом **в папке проекта** (там, где лежит `run.py`).

## Один раз

```
py -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

После `activate` в начале строки должно стоять `(.venv)`.

## Файл .env

```
copy .env.example .env
```

Заполните его (см. `SETUP_GUIDE.md`). Если у вас уже есть рабочий `.env` от прошлой папки, просто скопируйте его сюда.

## Проверки по порядку

```
python -m unittest test_booking_state test_agent_flows test_infra test_voice test_provider_compat
python run.py --mock --dry-run --simulate-time 12:00
python run.py --dry-run --simulate-time 12:00
```

1. Первая команда: в конце `OK`.
2. Вторая: без OpenAI. После `manager>` введите, например:
   `Good afternoon. Yes, we have a table for two at 5 pm today. You're speaking with The Ivy, Covent Garden.`
3. Перед третьей проверкой настройте `GEMINI_API_KEY` по `GEMINI_FREE_SETUP.md` и выполните `python check_provider.py`. Затем третья команда запустит настоящую модель Gemini в консольном режиме, без Telegram.

Выход из диалога: `/quit`.

## Про время

Бронь на **17:00 сегодня по Лондону**. Если это время уже прошло, агент не запустится и ничего не отправит.
`--simulate-time 12:00` нужен только для проверок с `--dry-run` или `--test`. Для настоящего менеджера он запрещён.

## Важно

- Каждый **новый терминал**: сначала `cd` в папку проекта, потом `.venv\Scripts\activate`.
- Вне окружения вместо `python` пишите `py`.
- `.env` никому не отправляйте и не выкладывайте на GitHub.
- Если ошибка: скопируйте текст целиком, без ключей.
