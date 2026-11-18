# Audit report: defects found in the previous version and how they were fixed

Source of the findings: an external code review (handoff document). Every finding was **re-checked against the actual code**
first: a script that reproduces each defect on the previous version (v2) ran before any change. All 7 were confirmed
(BUG-02 shares BUG-01's reproduction: confirmation passed with zero agreement).

| Bug | Confirmed on v2 | Fix | Proven by (tests) |
|---|---|---|---|
| BUG-01 `confirm_booking` accepted an invented restaurant | yes | Name is accepted only inside a quote found in a real manager message; the final text is built from verified state, tool arguments are only cross-checked | `TrustTests.test_bug01_*`, `RestaurantValidatorTests` |
| BUG-02 no programmatic check of the manager's agreement | yes | `manager_agreed` is set only by a validator (quote present; no refusal / other time / other party / other day; affirmation + slot cue) | `AgreementValidatorTests`, `ScenarioReadingTests`, `TrustTests.test_bug02_*` |
| BUG-03 a passed 17:00 only produced a warning | yes | Hard gate before any message (exit 3), second gate in `confirm_booking`, aware `Europe/London`, injectable clock | `TimeTests`, `TimeGateTests`, `RunProcessTests` |
| BUG-04 agent could stay active with no action and no timer | yes (both variants) | Bounded retries with back-off, reminders for a silent model, FAILED with a reason, timer rescheduled after every turn | `LivenessTests`, `ToolLoopTests` |
| BUG-05 mock used fragile keywords and a CLI-supplied restaurant | yes | Mock rewritten as a tool-using simulator that extracts the name from the manager's text; `--mock-restaurant` removed | `ScenarioTests` (all handoff section 8 scenarios) |
| BUG-06 log trusted a flag, not content | yes | Program logs real text after a successful send; model only attaches interpretation; failures are `send_failed` | `LogTruthTests` |
| BUG-07 final message had no calendar date | yes | Final text includes weekday, date, 17:00 London time, two guests, restaurant | `FinalTextTests`, `LogTruthTests.test_bug07_*` |

Also fixed during the work: `faster-whisper==1.0.3` needs `requests` (added); `openai==1.55.0` breaks with `httpx>=0.28` (pinned `httpx==0.27.2`);
Windows has no time zone database (`tzdata` added); a case-sensitive regex in the mock; a stale timer reference after the give-up.

## Test report

Environment: Python 3.12.3 (Linux), telethon 1.36.0, openai 1.55.0, httpx 0.27.2, python-dotenv 1.0.1, tzdata 2026.6; voice extras faster-whisper 1.0.3, edge-tts 6.1.12.
Clean virtual environment, installed from `requirements.txt`.

| Check | Command | Result |
|---|---|---|
| compile | `python -m py_compile *.py` | ok |
| core tests | `python -m unittest test_booking_state test_agent_flows test_infra` | 139 tests, OK |
| voice tests (stubbed TTS, real ffmpeg) | `python -m unittest test_voice` | 9 tests, OK |
| mutation check | each main safeguard removed in turn | 10 of 10 mutations made tests fail |
| offline demo as a process | `python run.py --mock --dry-run --simulate-time 12:00` | CONFIRMED, exit 0 |

**Offline only.** Not run: the real OpenAI model, a real Telegram login/send/receive, real edge-tts / faster-whisper, the real manager.
The Telegram adapter was tested against a fake client. Python 3.14 (the owner's version) was not available here.
