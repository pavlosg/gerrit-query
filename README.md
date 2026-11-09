# gerrit-query

Read-only command-line client for the Gerrit REST API (e.g. review.couchbase.org), with a Claude Code skill. It needs only `python3` (standard library) and works anonymously, with no credentials.

```
export GERRIT_HOST=review.couchbase.org      # or pass --host <hostname>
./gerrit-query.py query "project:kv_engine status:merged" -n 5
./gerrit-query.py change 257202              # or MB-69574, or a Change-Id
./gerrit-query.py comments 255786 --unresolved
./gerrit-query.py files 257202 --ps 1
./gerrit-query.py diff 257202 engines/ep/src/checkpoint_manager.cc
./gerrit-query.py show 257202 engines/ep/src/checkpoint_manager.cc --ps 1 --lines 100-160
./gerrit-query.py related 256816
```

Add `--json` to any command to get trimmed JSON. Run `./gerrit-query.py <command> -h` for each command's options.

## Read-only by construction

- Only HTTPS `GET` requests, and only to the configured host. Redirects to other hosts are refused.
- No subprocess or shell is used. Arguments are checked against strict patterns (change numbers, ticket keys, Change-Ids, file paths, a limited query character set) and URL-encoded.
- The command set is fixed (`change`, `comments`, `query`, `files`, `diff`, `show`, `related`). There are no write commands, and the script does not accept raw URLs or ssh commands.

## Tests

```
python3 -m unittest
```

`test_gerrit_query.py` runs offline. Commands run against a fake Gerrit that serves hand-written responses shaped like the REST API, and the HTTP error handling is tested through a fake opener. Run the tests after any change to the script.

## Claude Code setup

- Skill: `skills/gerrit-query/SKILL.md`, symlinked to `~/.claude/skills/gerrit-query`.
- In `~/.claude/settings.json`:
  ```json
  "permissions": { "allow": ["Bash(~/Documents/GitHub/gerrit-query/gerrit-query.py:*)"] },
  "env": { "GERRIT_HOST": "review.couchbase.org" }
  ```
