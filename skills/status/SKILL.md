---
name: status
description: Check the health of this project's Claude Code memory without any model call. Finds memories missing from the index, index entries pointing at nothing, near-duplicates, references to files that no longer exist, stale and oversized memories, and an index that is past the load limit. Use when the user asks "how is my memory", "memory health", "is my MEMORY.md too big".
---

# Memory status

Run:

```bash
rem status
```

If `rem` is not on PATH, use `"${CLAUDE_PLUGIN_ROOT}/bin/rem"`.

Give the user the score and the issues that matter, most serious first, in plain words. If there are issues or many sessions since the last dream, offer `/rem:dream`. If a dream is already waiting, offer `/rem:review`.
