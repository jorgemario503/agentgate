---
name: example-summarize
description: Summarize a text or log file into 5 bullet points, oldest-first, preserving error lines verbatim.
---

# Example skill (template)

This is an operator template for the open-model Agent Skills feature. It is only
loaded when `AGENTGATE_SKILLS_ENABLED=true` and a task is relevant; the full body
below is fetched on demand via the `skill_read` tool.

## Procedure

1. Read the target file with the `Read` tool.
2. Extract up to 5 key points, oldest-first.
3. Copy any line containing `ERROR` or `Traceback` verbatim into its point.
4. Return exactly 5 bullets, each prefixed with `- `.

## Notes

- Do not invent facts not present in the file.
- If the file is unreadable, report the error and stop.
