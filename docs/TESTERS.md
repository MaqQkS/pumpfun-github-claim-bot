# Telegram command access

Commands are open to everyone in every chat where GitClaim receives messages when
`RESEARCH_COMMANDS_ENABLED=true` in `research/research.env` (or the process environment).
There are no user/chat allowlists or blocklists. Legacy `RESEARCH_TESTER_IDS` and
`RESEARCH_CHAT_IDS` settings are ignored and can be removed.

Restart the bot after changing its configuration. Set `RESEARCH_COMMANDS_ENABLED=false`
to disable research commands globally. `/start` and `/stop` remain available to everyone.
Command access does not modify subscribers or `state.json`.

Replies go only to the requesting chat. Existing research budget limits, queue limits,
output scrubbing, and disabled/unimplemented command behavior still apply.
`AUTO_RESEARCH_CHAT_IDS` selects destinations for unsolicited automatic research;
it does not restrict who can use commands.
