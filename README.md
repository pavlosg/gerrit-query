# gerrit-query

Read-only command-line client for the Gerrit REST API (e.g. review.couchbase.org), with a Claude Code skill. It needs only `python3` (standard library) and works anonymously, with no credentials.

```
export GERRIT_HOST=review.couchbase.org      # or pass --host <hostname>
./gerrit-query.py query "project:kv_engine status:merged" -n 5
./gerrit-query.py change 257202              # or MB-69574, or a Change-Id
./gerrit-query.py comments 255786 --unresolved
./gerrit-query.py files 257202 --ps 1
./gerrit-query.py diff 257202 engines/ep/src/checkpoint_manager.cc
./gerrit-query.py related 256816
```

Add `--json` to any command to get trimmed JSON. Run `./gerrit-query.py <command> -h` for each command's options.

## Read-only by construction

- Only HTTPS `GET` requests, and only to the configured host. Redirects to other hosts are refused.
- No subprocess or shell is used. Arguments are checked against strict patterns (change numbers, ticket keys, Change-Ids, file paths, a limited query character set) and URL-encoded.
- The command set is fixed (`change`, `comments`, `query`, `files`, `diff`, `related`). There are no write commands, and the script does not accept raw URLs or ssh commands.

## Claude Code setup

- Skill: `skills/gerrit-query/SKILL.md`, symlinked to `~/.claude/skills/gerrit-query`.
- In `~/.claude/settings.json`:
  ```json
  "permissions": { "allow": ["Bash(~/Documents/GitHub/gerrit-query/gerrit-query.py:*)"] },
  "env": { "GERRIT_HOST": "review.couchbase.org" }
  ```
