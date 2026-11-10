---
name: rollback
description: Undo the last memory changes that rem applied, restoring every file to its exact previous content. Use when the user says "undo the dream", "roll back memory", "revert rem".
---

# Roll back

Confirm with the user that they want to undo the last apply, then run:

```bash
rem rollback
```

If `rem` is not on PATH, use `"${CLAUDE_PLUGIN_ROOT}/bin/rem"`.

If it reports files that were edited after the apply, list them and ask before running `rem rollback --force`, which overwrites those later edits.
