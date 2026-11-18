# Submission checklist

Tick a line only when it is really true. Do not tick what was not done.

## Proof from a REAL run (cannot be produced by the code or by an AI)

- [ ] Screenshot of the entire real conversation, taken from the agent's Telegram account
- [ ] `decisions.log` from that same real run (not a dry-run, not a template)
- [ ] The conversation really happened with the real manager (if it did not, say so in the notes)

## Repository

- [ ] GitHub link to the code (a real URL)
- [ ] The repository contains **no** `.env`, `*.session`, `*.session-journal`, `logs/`, `decisions.log` from tests, or API keys
      (check with `git status` and `git log -p | grep -i "api_key\|sk-"`)
- [ ] `.env.example` has only empty placeholders
- [ ] All code files are present: `run.py`, `booking_state.py`, `agent_core.py`, `tools_registry.py`, `decision_logger.py`,
      `telegram_client.py`, `mock_llm.py`, `prompts.py`, `config.py`, `utils.py`, `voice_utils.py`, `test_*.py`, `requirements*.txt`

## Written parts (in your own words)

- [ ] `SUBMISSION_NOTES.md` reviewed and personalised (5-10 lines plus an honest paragraph about failures/uncertainty)
- [ ] The paragraph "what broke and what I am unsure about" is written honestly
- [ ] If the real conversation was not completed, the notes say where it stopped and why

## Verified by you (see `TESTING.md`)

- [ ] Automated tests pass on your machine
- [ ] `--mock --dry-run` scenarios behave as described
- [ ] Real model dry-run checked (or marked as not checked)
- [ ] Test Telegram account run checked (or marked as not checked)
- [ ] Voice: working, or the notes say it was not verified

Before sending, rotate any key that might have been shared by mistake.
