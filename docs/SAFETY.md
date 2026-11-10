# Safety model

`rem` lets a model read your session transcripts and propose changes to files that are loaded into every future session. That is a place where mistakes repeat, so the design assumes the model can be wrong and that transcripts can contain hostile text.

## What is trusted and what is not

| Thing | Trusted? | Why |
|---|---|---|
| Your memory files and CLAUDE.md | Yes | They are already loaded into your sessions |
| Session transcripts | No | They contain web pages, issue comments, file contents and tool output that anyone could have written |
| Model output (findings, operations, revisions) | No | Models misread, overstate and can be steered by text in a transcript |
| The validator, apply and rollback code | Yes | Plain Python, covered by offline tests |

## What can be written, and where

An applied operation can change only:

1. Files directly inside the project's memory directory, with names like `some-name.md`. Never `MEMORY.md` by name (the index is edited line by line by code), never a path with a folder in it, never a symlink, never a name that differs from an existing file only by letter case.
2. The project's `CLAUDE.md`: one appended paragraph of at most 700 characters. Existing content is never rewritten, and the file's bytes and line endings are preserved.
3. A new `.claude/skills/<name>/SKILL.md`. An existing skill is never overwritten.

Each kind of operation is limited to the changes that kind is for. For example a `reindex` cannot write file content, and a `prune` cannot write anything.

## Evidence

- A session quote must appear inside one line of that session's digest (one message, one tool result). Matching ignores case, spacing, quote style and dash style, and nothing else. A quote joined with an ellipsis must have every part inside the same line, in order.
- The speaker of the matched line is recorded. A promotion to CLAUDE.md or to a skill requires at least one quote of the user's own words, and evidence from two sessions (or one session plus an existing memory that already recorded the lesson).
- A repository quote must appear in the named file inside the project directory. An empty quote with a path means "this path does not exist", and code confirms it.
- A new memory needs a session quote. A fix needs a session quote or repository evidence.

Evidence that fails is removed. An operation without the evidence its kind requires is rejected before you see it, and listed under "filtered out" with the reason.

## Prompt injection

Text in a transcript that addresses the agent ("save this to memory", "ignore previous instructions") is handled in layers:

- Every prompt tells the model that transcripts, memory files and project files are data.
- Tool output can never be the sole basis of a promotion, because promotions need the user's words.
- The second reader sees each quote with its speaker and the surrounding lines, and is asked to check whether words came from tool output.
- Whatever gets through still has to pass you: the proposal shows the full diff, including the index line, with control characters stripped.

This reduces the risk. It does not remove it: a `new` memory can rest on tool output (that is how hard-won facts are captured), so read new memories before applying them.

## Credentials

- Digests are redacted before any model call: common key formats (Anthropic, OpenAI, GitHub, GitLab, AWS, Google, Stripe, Slack, npm, Hugging Face), JWTs, private keys, bearer and basic auth values, passwords in URLs, and `password = value` style assignments in English and Polish, when the value looks like a secret.
- Proposed content containing a credential that is not already in your memory is rejected. Content containing the redaction mark itself is rejected, so a masked value cannot be written back as text.
- This is pattern matching. A secret in an unusual shape will pass. Digests are kept under `~/.claude/rem/` with owner-only permissions, and digests older than the last two runs are deleted.

## Apply and rollback

- A dream never changes your files. Only `rem apply` does.
- Before the first write, a snapshot folder and its manifest are created; the manifest is updated after every file. Files are replaced atomically.
- An operation is skipped if any file it touches has changed since the dream read it.
- `rem rollback` undoes the most recent apply, newest first. It restores exact bytes, removes files and folders that apply created, and refuses to overwrite a file you edited afterwards unless you pass `--force`.
- One dream or apply per project at a time (a lock file, released if its process is gone).

## Model calls

Each call runs `claude -p` as a fresh process with no hooks, no plugins, no MCP servers, no settings and no saved transcript. Analysis and the second read have no tools at all. The file review and consolidation steps have `Read`, `Glob` and `Grep` and nothing else, so they cannot write, run commands or reach the network.

## Reporting a problem

If you find a way to make `rem` write outside the places listed above, lose data on apply or rollback, or store a credential, please open an issue with a reproduction. A failing test in `tests/test_rem.py` is the most useful form.
