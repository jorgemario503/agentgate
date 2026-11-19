# AgentGate — resume block opening
#
# HOW THIS FILE IS READ
#   - Leading `#` comment lines at the top of this file are stripped and are
#     NEVER sent to the model. Documentation like this block costs nothing.
#   - A `#` line further down the file IS sent as ordinary text.
#   - The section heading ("## Resuming an existing session") is added by
#     AgentGate, not by this file. Do not add it here; it will be duplicated.
#   - Delete or empty this file to fall back to the built-in opening lines in
#     core/prompts.py (FALLBACK_RESUME).
#
# WHAT THIS FILE IS
#   Only the *opening lines* of the resume block are defined here. The
#   workspace path, the history file path, the format description, and the
#   last-two-turn digest are appended by AgentGate after this text and are not
#   configurable from this file.
#
#   This block is injected only when a non-Claude runner resumes a session
#   whose in-memory client is gone (COLD or RESUME_AFTER_TIMEOUT). It does not
#   apply to the Claude runner, which resumes through the SDK.
#
# PREFIX-CACHE RULE — please preserve it when editing
#   The resume block sits after the stable base rules and before per-session
#   memory, so it must stay identical for the life of a session. Do not add
#   timestamps, turn counters, or anything that changes between turns.

You are continuing a conversation that started earlier. Do not greet the
user or re-introduce yourself.
