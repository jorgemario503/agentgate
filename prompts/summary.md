# AgentGate — summarisation instructions
#
# HOW THIS FILE IS READ
#   - Leading `#` comment lines at the top of this file are stripped and are
#     NEVER sent to the model. Documentation like this block costs nothing.
#   - A `#` line further down the file IS sent as ordinary text.
#   - The section heading ("## Summarisation instructions") is added by
#     AgentGate, not by this file. Do not add it here.
#   - Delete or empty this file to fall back to the built-in text in
#     core/prompts.py (SUMMARY_PROMPT).
#
# WHAT THIS FILE IS
#   This prompt drives compaction: when a conversation grows past its budget,
#   older turns are condensed into a summary so they can leave the live
#   context. The summary is what the model will rely on for those turns, so
#   the instruction should favour keeping concrete technical detail over
#   brevity. It is not a security control.
#
#   This prompt is NOT part of the conversation prefix, so the prefix-cache
#   rule does not apply; it runs once per compaction against a transcript.

You are condensing an earlier portion of an ongoing coding-agent conversation so it can be removed from the live context. Preserve all information needed to continue the conversation correctly, including: user goals and constraints; decisions made and the reasoning behind them when it affects future work; concrete facts (file paths, function names, class names, APIs, identifiers, URLs, numbers, versions, commands, errors, and configuration values); code changes already made; remaining tasks, open questions, and known issues. Omit routine tool chatter, acknowledgements, repeated information, and other conversational filler. Be concise, but do not sacrifice important technical detail for brevity. Write plain prose with no headings or preamble.
