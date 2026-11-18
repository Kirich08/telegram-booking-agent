# Тестирование (по шагам)


## Status observed during final handoff (18 November 2026)

The submitter ran the 158-test command on Windows/Python 3.14. The run reported 3 skipped voice tests (FFmpeg unavailable) and 1 failure: the quota-error test expected the literal phrase `no credits`, while the implementation returned a semantically equivalent but differently worded message. The message text has been aligned in this cleaned package; rerun the full command before claiming a clean pass.

The submitter also ran `python check_provider.py` against `gemini-3.1-flash-lite`; it returned `PASS` for a multi-turn tool-call round-trip. A subsequent real-model console dry-run sent the opening message but did not complete the manager-reply turn: the model returned ordinary text without a tool call. Therefore, a complete booking dialogue is not verified.

Каждый шаг проверяет следующий. Не переходите дальше, пока предыдущий не прошёл.

## Что уже проверено автоматически (у меня, офлайн)

В исходной v3-сборке владелец прогнал `python -m unittest test_booking_state test_agent_flows test_infra test_voice -v`: 148 тестов. В этой Gemini-enabled версии добавлены 3 теста выбора конфигурации, 6 тестов адаптера tool-calling и 1 интеграционный тест истории агента (ожидаемый полный набор — 158). В текущей среде здесь отдельно выполнены 6 чистых тестов адаптера; полный набор нужно повторно запустить в вашей Windows `.venv`.
Они проверяют: время с поддельными часами (16:59:59, 17:00:00, 17:01, лето/зима), доказательства ресторана и
согласия, все сценарии менеджера (согласие, отказ, альтернатива, неоднозначное «yes», просьба имени/телефона,
prompt-injection, молчание, повтор события, несколько сообщений подряд), зависания, ошибки API, точность лога,
Telegram-адаптер на подменённом клиенте и запуск `run.py` как процесса.

**Не проверено автоматически:** реальная модель Gemini/OpenAI, настоящий вход и отправка в Telegram, настоящий голос. Для реальной проверки Gemini без Telegram предусмотрен `check_provider.py`.
Это проверяете вы по шагам ниже.

## Этап 0. Автотесты у вас

```
python -m unittest test_booking_state test_agent_flows test_infra test_voice test_provider_compat -v
```

В конце: `OK`. Если что-то падает, пришлите текст ошибки целиком.

## Этап 1. Без ключей и без Telegram

```
python run.py --mock --dry-run --simulate-time 12:00
```

Без `--simulate-time` после 17:00 по Лондону агент **откажется запускаться** и ничего не отправит. Это правильно.

Введите после `manager>` по очереди (каждый раз новый запуск):

1. `Good afternoon. Yes, we have a table for two at 5 pm today. You're speaking with The Ivy, Covent Garden.`
   Ожидаем: бронь подтверждена, в тексте дата, 17:00, двое гостей и The Ivy. Итог `CONFIRMED`.
2. `Yes, we can accommodate two guests at 5 pm today.` Ожидаем: агент **спрашивает название ресторана**, а не подтверждает.
3. `Yes, we do have a restaurant. How can I help?` Ожидаем: уточняющий вопрос, не подтверждение.
4. `We are fully booked at 5 pm, but we can offer 5:30 pm.` Ожидаем: вежливый отказ от другого времени, итог `DECLINED`.
5. `May I have the name for the reservation?` Ожидаем: агент **не придумывает имя**, а говорит, что его нет.

Проверьте `decisions.log`: у каждого сообщения одна строка, текст точный, у входящих есть `message_id`.

## Этап 2. Настоящая модель, без Telegram

Можно использовать бесплатный Gemini API key из Google AI Studio: см. `GEMINI_FREE_SETUP.md`. Сначала выполните отдельный тест совместимости (он проверяет два последовательных запроса с вызовом функции и не обращается к Telegram):

```
python check_provider.py
```

Если он завершился `PASS`, проверьте реальную модель в консольном режиме: `python run.py --dry-run` (или `--dry-run --simulate-time 12:00`, если 17:00 в Лондоне уже прошло). Вводите ответы только после приглашения `manager>` внутри процесса.

Повторите те же реплики менеджера. Смотрите: первое сообщение вежливое, на английском; ресторан не назван,
пока его не назвали вы; нет двух сообщений подряд; в `decisions.log` в колонках понимания и решения нет `(not provided by the model)`
(если много, модель плохо вызывает `log_decision`, это не ломает бронь, но ухудшает лог).
Если итог `FAILED`, прочитайте причину в консоли.

## Этап 3. Настоящий Telegram, тестовый аккаунт

```
python run.py --test
```

Проверьте: вход по коду, сообщение пришло на тестовый аккаунт, ваши ответы доходят до агента, подтверждение
приходит в чат, программа завершилась сама. После 17:00 по Лондону добавьте `--simulate-time 12:00`.
Проверьте также Ctrl+C: программа должна остановиться без зависания.

## Этап 4. Реальный менеджер

Только когда этапы 1-3 прошли и вы сами решили. Запуск без флагов: `python run.py`.
**Одна попытка. Только до 17:00 по Лондону** (до 19:00 по Киеву зимой). После 17:00 агент не запустится.
После окончания сохраните скриншот чата из аккаунта агента и `decisions.log`.

## Проверка follow-up

```
python run.py --test --follow-up-minutes 1 --max-follow-ups 2
```

Не отвечайте. Через минуту придёт напоминание, ещё через минуту второе, затем итог `NO_RESPONSE`.

## Проверка голоса (если включаете)

См. `VOICE.md`. Автотесты проверяют только конвертацию и откаты; настоящий синтез и распознавание вы проверяете сами.
