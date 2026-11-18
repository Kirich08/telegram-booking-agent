# Implementation notes

1. **Tools considered:** Telegram Bot API, Telethon and Pyrogram for Telegram; OpenAI-compatible APIs and Google Gemini for the language model.
2. **What I chose and why:** Telethon is used because this task requires the agent to initiate a conversation with a Telegram user. The OpenAI Python SDK is used as the HTTP client, configured with Google's Gemini OpenAI-compatible endpoint to avoid requiring OpenAI billing.
3. **Model used:** `gemini-3.1-flash-lite`. The provider diagnostic completed a multi-turn function-calling round-trip on my machine.
4. **Architecture:** The model proposes actions through tool calling; Python code validates the manager's evidence, booking state, 17:00 Europe/London time gate and final confirmation before accepting a booking.
5. **How AI was used:** Claude was used during implementation. ChatGPT was used for an independent code review and debugging support. I reviewed the reported issues and ran local tests and provider diagnostics.
6. **Automated testing:** The latest local run executed 158 tests. Three voice-related tests were skipped because FFmpeg was not installed. One test failed because the expected quota-error wording did not match the updated message; this wording has been aligned in this repository and needs a clean rerun.
7. **What I checked manually:** The Gemini provider diagnostic passed. A real-model dry-run produced the opening message, but did not complete the manager-reply turn successfully.
8. **Not verified:** A real Telegram login and send/receive cycle, a real conversation with the manager, and end-to-end booking confirmation have not been completed. Voice functionality has not been verified with the real speech services.

## What broke and what I am unsure about

The earlier versions had defects in booking validation, manager-agreement checks, time gating, retry/liveness handling, mock behavior, logging and final confirmation details; these were addressed with validation and regression tests. The Gemini provider check now passes, but during a real-model dry-run the model returned ordinary text instead of the required tool call after the manager's reply, so a complete booking was not demonstrated. I have not authenticated the Telethon client against Telegram or tested a real message exchange with the manager. The code uses conservative text-based validators, so some legitimate manager replies may be rejected and require clarification. I therefore cannot claim that a real booking was made or confirmed.
