---
name: schedule
description: Turn nightly dreaming on or off. The nightly job reviews every project that has new sessions and leaves proposals for review; it never applies anything. Use when the user says "dream every night", "schedule dreaming", "turn off nightly dreams".
argument-hint: "[install|uninstall|status]"
---

# Nightly dreams

```bash
rem schedule status
rem schedule install      # every night at 03:00 (add --hour 5 to change)
rem schedule uninstall
```

If `rem` is not on PATH, use `"${CLAUDE_PLUGIN_ROOT}/bin/rem"`.

Before installing, tell the user what it does: once a night it runs a dream for each project with at least three new sessions (five projects at most), using their Claude Code account. It only writes proposals. The next session in that project mentions that a dream is waiting, and they review it with `/rem:review`. The machine has to be awake at that hour; on macOS a missed run happens at the next wake.

Install only after they agree.
