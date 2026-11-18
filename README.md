# Booking Agent: a Telegram userbot that books a table

An AI agent that starts a Telegram conversation with a London restaurant manager and tries to
book a table for **two at 17:00 today (Europe/London)**. It writes its own messages, reacts to the
manager's replies, never states a restaurant the manager did not name, and logs every message.

## The idea: the model proposes, the code decides

The model (function calling through OpenAI or an OpenAI-compatible provider such as Gemini) reads the manager's messages and proposes the next action.
It is **not** trusted to say what is true. A separate layer, `booking_state.py`, keeps the verified facts:

- **Restaurant name**: accepted only if it appears literally inside a quote that is itself
  found in a real manager message. The final confirmation is built from this verified name,
  never from an argument the model passes.
- **Agreement**: accepted only if the manager's message really contains an affirmation about the
  table/time/party, and does *not* contain a refusal, another time, another party size or another day.
  A bare "yes" counts only as an answer to a message that stated the exact slot.
- **Time**: `17:00` today in London is computed with an aware `Europe/London` clock. At 17:00:00 or later
  the agent refuses to start (nothing is sent) and `confirm_booking` refuses too.
- **Transport log**: incoming and outgoing messages are logged by the program with their exact text.
  The model can only add its interpretation; it cannot change what was logged. A failed send is logged as `send_failed`.

When something is unclear, the checks refuse and the agent asks a short clarifying question.
These checks are conservative text rules, **not language understanding**: they can refuse a valid
reply (then the agent asks again), and they cannot catch every phrasing a human might use.

## Conversation states

`NEW → PROCESSING ⇄ WAITING_FOR_MANAGER → CONFIRMED | DECLINED | EXPIRED | NO_RESPONSE | FAILED | CANCELLED`

An active conversation always has a way forward: a follow-up timer, a Telegram listener, or a bounded
retry. A model that returns no tool call is reminded twice, then the run ends as `FAILED` with a reason.
Temporary API errors are retried with back-off; a wrong key, missing permission or empty balance ends the run
at once with a clear message. After the last follow-up the agent waits once more and ends as `NO_RESPONSE`.

## Tools given to the model

`send_message`, `record_manager_reply`, `log_decision`, `confirm_booking`, `end_conversation`, `get_current_time`.
The first and the third plus `confirm_booking` and `get_current_time` come from the assignment text;
`record_manager_reply` (report what you understood, with exact quotes) and `end_conversation`
(finish after a decline) were added. At most one message is sent per turn.

## Repository layout

| File | Purpose |
|---|---|
| `run.py` | Entry point: flags, time gate, wiring, exit codes |
| `booking_state.py` | Verified state, statuses, time rules, evidence validators |
| `agent_core.py` | Turn loop, retries, no-action handling, follow-up timer |
| `tools_registry.py` | Tool schemas and implementations (confirmation guard) |
| `decision_logger.py` | `decisions.log` (TSV): transport facts by the program, interpretation by the model |
| `telegram_client.py` | Telethon wrapper: login, sending, FloodWait retries, listener filter |
| `mock_llm.py` | Offline simulated model (`--mock`); extracts the name from the manager's text |
| `prompts.py`, `SYSTEM_PROMPT.md` | Assignment prompt (verbatim) plus an extra safety-rules block |
| `config.py`, `utils.py` | Settings validation, London time |
| `voice_utils.py`, `VOICE.md` | Optional voice (see limits below) |
| `test_*.py` | Automated tests (no keys, no network) |

## Quick start

```bash
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python -m unittest test_booking_state test_agent_flows test_infra test_voice test_provider_compat
python run.py --mock --dry-run --simulate-time 12:00     # offline demo, no keys
```

## Running

| Command | What it does |
|---|---|
| `python run.py --mock --dry-run` | Offline: simulated model, you type the manager's replies. Needs no keys. |
| `python check_provider.py` | Verifies Gemini tool calling and one multi-turn tool round-trip; no Telegram. |
| `python run.py --dry-run` | Real configured model provider, no Telegram. You type the manager's replies. |
| `python run.py --test` | Real Telegram, writes to `TEST_MANAGER_USERNAME` (your second account). |
| `python run.py` | Real Telegram, writes to `MANAGER_USERNAME`. **Only when you are ready; one run = one attempt.** |

`--simulate-time HH:MM` pretends it is that London time today. It exists so you can test after 17:00 and is
refused unless `--dry-run` or `--test` is used. Other flags: `--voice`, `--follow-up-minutes N`, `--max-follow-ups N`,
`--reset`, `--logout`, `--list-tools`, `--system-prompt`, `--debug`, `--version`.

Exit codes: `0` confirmed or declined, `1` failed, `2` bad configuration, `3` 17:00 passed or no reply, `130` stopped.

## Configuration (`.env`, template in `.env.example`)

| Variable | Needed for | Default | Meaning |
|---|---|---|---|
| `TELEGRAM_API_ID`, `TELEGRAM_API_HASH`, `TELEGRAM_PHONE` | real Telegram | | From my.telegram.org; phone of the Telegram user account that sends messages (a dedicated account is safer) |
| `MANAGER_USERNAME` | real Telegram | | e.g. `@vlad_dev_7` |
| `TEST_MANAGER_USERNAME` | optional `--test` mode | | Optional test recipient; must differ from `MANAGER_USERNAME` |
| `GEMINI_API_KEY` | Gemini model | | Optional free-tier key from Google AI Studio; preferred when set |
| `GEMINI_MODEL` | no | `gemini-3.1-flash-lite` | Gemini model ID; must support function calling |
| `GEMINI_BASE_URL` | no | Google Gemini OpenAI-compatible endpoint | Defaults automatically when `GEMINI_API_KEY` is set |
| `OPENAI_API_KEY` | OpenAI model | | Not needed with `--mock` or Gemini |
| `OPENAI_MODEL` | no | `gpt-4o-mini` | OpenAI model ID; must support function calling |
| `OPENAI_BASE_URL` | no | OpenAI | Optional compatible endpoint if Gemini is not selected |
| `FOLLOW_UP_MINUTES` | no | `15` | At least 1 |
| `MAX_FOLLOW_UPS` | no | `2` | 0 or more |
| `ENABLE_VOICE` | no | `false` | Voice also needs `--voice` |

## Output files

`decisions.log` (TSV, header `timestamp direction message understood decided message_id`, one row per real message; directions:
`sent`, `received`, `sent_dry_run`, `send_failed`), `logs/agent.log`, and `booking_agent.session` (**secret, never commit**).

## Free model provider

When OpenAI Billing is unavailable, configure `GEMINI_API_KEY` from Google AI Studio. The project uses the official Gemini OpenAI-compatible endpoint. Read `GEMINI_FREE_SETUP.md`, then run `python check_provider.py` before the real-model dry run. Free-tier limits and availability can change, and Google states that Free-tier content may be used to improve its products; do not send confidential data.

## What has and has not been verified

Verified offline (see `TESTING.md` for counts): the state machine, validators, time boundaries with a fake clock,
all scenarios in `test_agent_flows.py`, retry and no-action handling, logging, the Telegram adapter against a **fake**
client, and `run.py` as a process. A mutation check confirmed the tests fail when each main safeguard is removed.

**Not verified in this package without the owner's key**: live Gemini/OpenAI model behaviour, real Telegram login/send/receive, real `edge-tts` and `faster-whisper`, how Telegram shows a caption under a voice note, and the real manager's replies. `check_provider.py` is included to verify the configured Gemini account's multi-turn tool round-trip before running the full agent; it still needs the owner's API key.

## Verification status at submission

- The Gemini provider diagnostic (`python check_provider.py`) completed successfully with `gemini-3.1-flash-lite` on the submitter's Windows machine. It verifies a multi-turn tool-call round-trip only; it does **not** test Telegram.
- A real-model console dry-run generated the opening message, but the subsequent manager-reply turn returned plain text without a tool call. The full booking dialogue is therefore **not verified**.
- The submitter's latest Windows test run executed 158 tests: 3 were skipped because FFmpeg was unavailable, and 1 failed because a test expected the old `no credits` wording. The quota message has been aligned in this cleaned repository, but the full suite must be rerun before claiming all tests pass.
- Real Telegram login, send/receive, a conversation with the manager, and voice processing were not verified.

Do not present a dry-run or mock transcript as proof of a real booking.

## Limitations and decisions

- Alternative times are not accepted and no operator approval channel exists: the agent thanks the manager and ends.
- If the manager asks for a guest name or phone, the agent says it does not have it and asks whether the booking can be held
  without it. It does not promise to send it later (nothing could). It prints a warning for the operator.
- A restaurant name the manager never writes in a message cannot be confirmed; the agent asks for it.
- `send_message` text itself is not screened: a model could still write a misleading sentence. Only the final confirmation and
  the state are guarded.
- Voice is optional and off by default.

## Security

Using a **dedicated Telegram account** is safer than using your personal account. `.env` and `*.session` are secrets and are in `.gitignore`; check `git status` before every commit. `TEST_MANAGER_USERNAME` is optional and only needed for the separate `--test` recipient mode.
Messages from the manager are treated as data: the prompt tells the model to ignore instructions inside them, and the code never
gives the model access to secrets or a shell.

## Troubleshooting

| Symptom | Fix |
|---|---|
| `17:00 today has already passed` | Run before 17:00 London time, or add `--simulate-time 12:00` with `--dry-run`/`--test`. |
| `429` / quota exhausted | Check the selected provider's rate limits/usage page; for Gemini see AI Studio. Wait for quota reset or use a provider with available quota. |
| `Configuration error: Missing environment variables` | Create `.env` next to `run.py` and fill the listed variables. |
| `The manager username does not exist.` | Check the username spelling. |
| `PeerFloodError` | Telegram limits new accounts from writing first; use an older account or write once by hand. |
| `Cannot message the manager due to privacy settings.` | The manager restricts who may write. |
| `time zone database is missing` | `pip install tzdata` (it is in `requirements.txt`). |

## Submission files

- `SUBMISSION_NOTES.md` records what was tested and what remains unverified.
- `GITHUB_PUBLISH_STEPS.md` explains how to publish the cleaned repository without committing secrets.
- `SUBMISSION_CHECKLIST.md` lists the evidence still required for a complete real-world submission.
