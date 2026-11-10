---
name: gerrit-query
description: Read Gerrit code review data (e.g. Couchbase's review.couchbase.org) with a read-only CLI. Use when looking up Gerrit changes, review or inline comments, CV / Verified / Code-Review results, the diff or files of a change, a change's series or cherry-picks, or the changes for a ticket (MB-12345 or any Jira key).
---

# gerrit-query

`~/Documents/GitHub/gerrit-query/gerrit-query.py` is a read-only, anonymous Gerrit client. It cannot review, vote, submit, abandon, rebase, set reviewers or post comments. For those, send the user to the Gerrit web UI.

## How to invoke

Run it exactly like this, as a single command:

```
~/Documents/GitHub/gerrit-query/gerrit-query.py <command> [args]
```

- Do not prefix it with `python3`, `cd` into the repo, pipe its output, or chain it with `&&`/`;`. The permission rule matches only the plain form. The output is already trimmed.
- The host comes from `GERRIT_HOST` (`review.couchbase.org`). For another server, pass `--host <hostname>`: a bare hostname, never a URL.
- `--json` prints trimmed JSON. Use it only for fields the text output doesn't show.

## Commands

| Command | Use it for |
|---|---|
| `change <number\|TICKET-123\|Change-Id> [-n N] [--max-msg N] [--no-comments] [--no-related] [--since-ps N] [--after DATE]` | Everything about one change: header, parent, votes, commit message, files, sibling changes, and messages with their review comments. A ticket or Change-Id with several matches prints a list to pick from. |
| `comments <number> [--unresolved] [--since-ps N] [--after DATE]` | Review comments threaded by file, with the code each thread is on. |
| `query "<gerrit query>" [-n N]` | One line per matching change (default 25, max 100). |
| `files <number> [--ps N]` | Changed files with +/- counts. |
| `diff <number> [file] [--ps N] [--base M] [--context C] [--max-lines L] [--no-line-numbers]` | Unified diff. `--base M` diffs patchset M against `--ps` (or current). |
| `show <number> <file> [--ps N] [--lines A-B] [--max-lines L]` | A whole file at a patchset. Use this, not `diff --context` or a `git fetch`, to read a file in full. |
| `related <number>` | The series (newest first, `*` = this change), then same Change-Id and same-topic changes. |

## Flags and output that aren't self-explanatory

- `--since-ps N` keeps what was posted after patchset N was uploaded, including late replies to older patchsets' comments. `--after "YYYY-MM-DD[ HH:MM]"` is UTC. In `comments`, matching threads are shown whole with new comments marked `(new)`.
- `--max-msg N` cuts messages and comments to N characters (default 800, `0` = full). `--no-comments` also drops `>` lines, where the author quotes earlier text.
- `--no-comments` and `--no-related` each skip extra requests. `--no-related` drops the sibling lists and parent tags. Pass both when you need only the header, votes, commit message, files and messages, e.g. to check CV on many changes. The skipped parts are still available from `comments` and `related`.
- `[thread resolved|UNRESOLVED]` on a thread's first comment is the whole thread's final state, not that comment's own flag.
- In list lines, `V`/`CR` is the lowest vote if any is negative, otherwise the highest. `-` means no votes.
- Parent tags in `change`: `[change N, NEW]` means it is stacked on another open change. `[<branch> tip is now <sha>]` only means it is behind the branch, which is not a problem by itself. With `--no-related` there are no tags.
- In `related`, `[series has PS5]` means the chain uses an older patchset than the current one. `[on … of N, current is PSm: needs rebase]` means that entry needs a rebase.
- A cross-repo series is tied together by a shared Change-Id or a shared topic, so check both lists.
- In `diff`, the second number column is the new line number. That is the one a comment's `file:line` refers to, and the one to use when drafting an inline comment.
- CV results: the `Verified` label and Build Bot messages. Failed jobs appear as `<jenkins url> : FAILURE`. Runs of plain "Build Started" messages are collapsed into one line (`Build Started xN`).

Query strings may contain only letters, digits, spaces and `_.:/+-~"(),=<>@*`. Common operators: `project:kv_engine`, `status:open|merged|abandoned`, `branch:master`, `owner:"Name"`, `reviewer:`, `message:MB-12345`, `topic:`, `file:`, `-is:wip`, `after:2026-01-01`, `label:Verified=-1`, `has:unresolved`.

## Examples

```
~/Documents/GitHub/gerrit-query/gerrit-query.py query "project:kv_engine status:open label:Verified=-1" -n 10
~/Documents/GitHub/gerrit-query/gerrit-query.py change MB-69574
~/Documents/GitHub/gerrit-query/gerrit-query.py comments 255786 --unresolved --since-ps 3
```
