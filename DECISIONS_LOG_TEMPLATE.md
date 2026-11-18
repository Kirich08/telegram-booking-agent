# Decision log: how to read it (this is NOT a real log)

The real log is `decisions.log`, a TSV file written by the program. Columns are separated by tabs.
Open it in a spreadsheet program.

Header:
```
timestamp	direction	message	understood	decided	message_id
```

- `direction`: `received` (from the manager), `sent` (really sent), `sent_dry_run` (printed, not sent),
  `send_failed` (the transport failed; the text was NOT delivered).
- `message`: the **exact** text. It is recorded by the program, not by the model.
- `understood` / `decided`: the model's own interpretation, attached to that message. If the model gave none, the
  cell says `(not provided by the model)`. A note is added when the model quoted a different text than the real one.
- `message_id`: the Telegram message id for incoming messages (`tg-<id>`); `console-<n>` in dry-run.
- Each real message has exactly one row, in the order it happened.

Never present this template, or a dry-run log, as a real conversation.
