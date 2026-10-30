---
name: gerrit-query
description: Read Gerrit code review data (e.g. Couchbase's review.couchbase.org) with a read-only CLI. Use when looking up Gerrit changes, review or inline comments, CV / Verified / Code-Review results, the diff or files of a change, a change's series or cherry-picks, or the changes for a ticket (MB-12345 or any Jira key).
---

# gerrit-query

`~/Documents/GitHub/gerrit-query/gerrit-query.py` is a read-only Gerrit client. It sends only anonymous HTTPS GET requests, accepts no URLs or write verbs, and does its own JSON trimming.

## How to invoke

Run it exactly like this, as a single command:

```
~/Documents/GitHub/gerrit-query/gerrit-query.py <command> [args]
```

- Do not prefix it with `python3`, `cd` into the repo, pipe its output, or chain it with `&&`/`;`. The permission rule matches only the plain form, so anything else triggers a prompt. The script already trims its output, so there is nothing to post-process.
- The host comes from `GERRIT_HOST` (set to `review.couchbase.org` in the Claude settings). For another Gerrit server, pass `--host <hostname>`: a bare hostname, never a URL.
- `--json` (before or after the command) prints trimmed JSON in place of text. Use it only when you need fields the text output doesn't show.

## Commands

| Command | What it shows |
|---|---|
| `change <number\|TICKET-123\|Change-Id> [-n N] [--max-msg N]` | Subject, project/branch, status, owner, dates, current patchset, labels with every non-zero vote (Verified = CV, Code-Review, …), full commit message, files, and all change messages. Each message is cut to N chars (default 800, `0` = full); runs of plain CV "Build Started" notices are collapsed into one line. A ticket key or Change-Id that matches several changes (backports, follow-ups, other repos) prints the list (up to `-n`, default 50, max 100). Pick a number from it and run `change <number>`. |
| `comments <number> [--unresolved]` | Inline and patchset-level comments grouped by file, threaded by reply: `PS<n> L<line> [resolved\|UNRESOLVED] author date`. `--unresolved` keeps only threads whose last comment is unresolved. |
| `query "<gerrit query>" [-n N]` | One line per change: number, status, updated, owner, project/branch, subject. Default 25 results, max 100. |
| `files <number> [--ps N]` | Changed files with status and +/- for the current or a given patchset. |
| `diff <number> [file] [--ps N] [--base M] [--context C] [--max-lines L]` | Unified diff of the whole change or one file. `--base M` diffs patchset M against `--ps` (or current). Output is capped at 2000 lines by default, so pass a file to narrow it. |
| `related <number>` | The change's series (dependency chain, `*` marks this change) and the same Change-Id on other branches (cherry-picks/backports). |

Query strings may contain only letters, digits, spaces and `_.:/+-~"(),=<>@*`. Common operators: `project:kv_engine`, `status:open|merged|abandoned`, `branch:master`, `owner:"Name"`, `reviewer:`, `message:MB-12345`, `topic:`, `file:`, `-is:wip`, `after:2026-01-01`, `label:Verified=-1`, `has:unresolved`.

## Examples

```
~/Documents/GitHub/gerrit-query/gerrit-query.py query "project:kv_engine status:open label:Verified=-1" -n 10
~/Documents/GitHub/gerrit-query/gerrit-query.py change MB-69574
~/Documents/GitHub/gerrit-query/gerrit-query.py change 257202 --max-msg 0
~/Documents/GitHub/gerrit-query/gerrit-query.py comments 255786 --unresolved
~/Documents/GitHub/gerrit-query/gerrit-query.py diff 257202 engines/ep/src/checkpoint_manager.cc
~/Documents/GitHub/gerrit-query/gerrit-query.py related 256816
```

CV results: in `change`, look at the `Verified` label and the Build Bot messages. Failed jobs appear as `<jenkins url> : FAILURE`.

## Read-only

The tool cannot review, vote, submit, abandon, rebase, set reviewers or post comments, and it has no credentials. If the user wants one of those actions, tell them to do it in the Gerrit web UI.
