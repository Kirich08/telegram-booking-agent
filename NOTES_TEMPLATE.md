# Notes (5–10 lines) and the failure paragraph

Fill in the brackets. Keep it short and honest. Replace the sample text with your own.

## Notes (5–10 lines)

1. **Tools and libraries I considered:** [e.g. Telegram Bot API, Telethon, Pyrogram; OpenAI API, local models via Ollama, OpenRouter]
2. **What I chose and why:** Telethon, because a Bot API bot cannot write to a user first. OpenAI gpt-4o-mini with function calling, because it is cheap and reliable for short conversations.
3. **Why this structure:** the model proposes actions and calls tools; a separate verified state decides what is true (the restaurant name and the agreement must come from quotes found in real manager messages, the 17:00 London slot must not have passed, one message per turn, bounded retries and follow-ups).
4. **How I tested:** [e.g. offline mock run, dry run with a real model, a second Telegram account, then the real manager]
5. **How I used AI:** [e.g. I used an AI assistant to generate the first version of the code; I read every file, ran the tests and fixed the bugs I found myself]
6. **What I checked by hand:** [e.g. the confirmation message contains the correct time, date, party size and restaurant name]
7. **Limits of the choice:** [e.g. the userbot approach can break Telegram's rules; I used a separate account]

## What broke and what I am unsure about

[Write one paragraph. Say what did not work, what you fixed, and what you still doubt. Example: "The first version of the confirmation tool accepted an empty restaurant name, so the agent could confirm a booking without knowing the restaurant. I added a check that refuses it. I am not sure how the real manager will reply to a voice message, because voice is not implemented; the agent asks the manager to write instead. I also have not verified how gpt-4o-mini behaves over a long conversation."]
