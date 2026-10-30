#!/usr/bin/env python3
"""Read-only Gerrit CLI.

Only issues anonymous HTTPS GET requests to a Gerrit REST API. Every argument
is validated against a conservative pattern before it becomes part of a URL;
there is no way to pass a raw URL, an ssh command or a write operation.

Host: --host <hostname> or the GERRIT_HOST environment variable.
"""
import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request

TIMEOUT = 20
XSSI_PREFIX = ")]}'"

HOST_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?(?::\d{1,5})?$")
NUMBER_RE = re.compile(r"^\d{1,9}$")
TICKET_RE = re.compile(r"^[A-Za-z][A-Za-z0-9]{0,15}-\d{1,7}$")
CHANGE_ID_RE = re.compile(r"^I[0-9a-f]{40}$")
QUERY_RE = re.compile(r'^[A-Za-z0-9 _.:/+\-~"(),=<>@*]{1,300}$')
PATH_RE = re.compile(r"^[A-Za-z0-9_./+@-]{1,400}$")

ACCOUNT_PLACEHOLDER_RE = re.compile(r"<GERRIT_ACCOUNT_(\d+)>")
BUILD_STARTED_RE = re.compile(r"^(Patch Set \d+:\n\n)?Build Started \S+ \(\d+/\d+\)\s*$")

DROP_KEYS = {"avatars", "fetch", "attention_set", "removed_from_attention_set",
             "requirements", "submit_records", "meta_rev_id", "triplet_id",
             "virtual_id_number", "permitted_labels", "removable_reviewers",
             "removable_labels", "reviewer_updates", "trackingids"}


class UsageError(Exception):
    pass


# ---------------------------------------------------------------- validation

def _validator(pattern, what):
    def check(value):
        if not pattern.match(value):
            raise argparse.ArgumentTypeError(f"invalid {what}: {value!r}")
        return value
    check.__name__ = what
    return check


change_number = _validator(NUMBER_RE, "change number")
query_string = _validator(QUERY_RE, "query string (allowed: letters, digits, space and _.:/+-~\"(),=<>@*)")


def change_ref(value):
    if NUMBER_RE.match(value) or CHANGE_ID_RE.match(value):
        return value
    if TICKET_RE.match(value):
        return value.upper()
    raise argparse.ArgumentTypeError(
        f"invalid change reference {value!r}: expected a change number, "
        "a ticket key like MB-12345, or a Change-Id (I + 40 hex chars)")


def file_path(value):
    if not PATH_RE.match(value) or ".." in value.split("/") or value.startswith("/"):
        raise argparse.ArgumentTypeError(f"invalid file path: {value!r}")
    return value


def host_name(value):
    if not HOST_RE.match(value):
        raise argparse.ArgumentTypeError(
            f"invalid host {value!r}: give a bare hostname such as review.couchbase.org")
    return value


def bounded_int(lo, hi):
    def check(value):
        if not re.match(r"^\d{1,6}$", value) or not lo <= int(value) <= hi:
            raise argparse.ArgumentTypeError(f"expected an integer in {lo}..{hi}, got {value!r}")
        return int(value)
    return check


# ---------------------------------------------------------------------- HTTP

class _SameHostRedirect(urllib.request.HTTPRedirectHandler):
    def __init__(self, host):
        self.host = host

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        parsed = urllib.parse.urlsplit(newurl)
        if parsed.scheme != "https" or parsed.netloc != self.host:
            raise urllib.error.URLError(f"refusing redirect to {newurl}")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class Gerrit:
    def __init__(self, host):
        self.host = host
        self.opener = urllib.request.build_opener(_SameHostRedirect(host))

    def get(self, path, params=None):
        url = f"https://{self.host}{path}"
        if params:
            url += "?" + urllib.parse.urlencode(params, doseq=True)
        req = urllib.request.Request(url, method="GET",
                                     headers={"Accept": "application/json"})
        try:
            with self.opener.open(req, timeout=TIMEOUT) as resp:
                body = resp.read().decode("utf-8")
        except urllib.error.HTTPError as e:
            if e.code == 404:
                raise UsageError(f"not found: {path}")
            raise UsageError(f"HTTP {e.code} for {path}")
        except urllib.error.URLError as e:
            raise UsageError(f"request failed: {e.reason}")
        if body.startswith(XSSI_PREFIX):
            body = body[len(XSSI_PREFIX):]
        return json.loads(body)


def change_path(number, *rest):
    return "/changes/" + "/".join([number, *rest])


# ------------------------------------------------------------------ helpers

def who(account):
    if not account:
        return "?"
    return account.get("name") or account.get("username") or account.get("email") \
        or f"#{account.get('_account_id', '?')}"


def day(ts):
    return (ts or "")[:10]


def minute(ts):
    return (ts or "")[:16]


def trim(obj):
    """Drop bulky/noisy keys and collapse accounts to names."""
    if isinstance(obj, dict):
        if "_account_id" in obj and set(obj) <= {"_account_id", "name", "email", "username",
                                                 "avatars", "display_name", "tags"}:
            return who(obj)
        return {k: trim(v) for k, v in obj.items() if k not in DROP_KEYS}
    if isinstance(obj, list):
        return [trim(v) for v in obj]
    return obj


def clip(text, limit):
    text = (text or "").rstrip()
    if limit and len(text) > limit:
        return text[:limit].rstrip() + f" …[+{len(text) - limit} chars]"
    return text


def indent(text, prefix):
    return "\n".join(prefix + line for line in text.splitlines()) if text else prefix


def emit_json(data):
    print(json.dumps(trim(data), indent=1, ensure_ascii=False))


def query_changes(g, q, limit, extra_options=()):
    return g.get("/changes/", [("q", q), ("n", limit), ("o", "DETAILED_ACCOUNTS"),
                               *[("o", o) for o in extra_options]])


def print_change_list(changes):
    for c in changes:
        print(f"{c['_number']:>7}  {c['status']:<9} {day(c.get('updated'))}  "
              f"{who(c.get('owner')):<22.22} {c['project']}/{c['branch']}  {c['subject']}")
    if changes and changes[-1].get("_more_changes"):
        print("(more results available; raise -n)")


def resolve_number(g, ref):
    """Map a change ref to a single change number, or print candidates and return None."""
    if NUMBER_RE.match(ref):
        return ref
    q = f"change:{ref}" if CHANGE_ID_RE.match(ref) else f"message:{ref}"
    hits = query_changes(g, q, 50)
    if not hits:
        raise UsageError(f"no changes match {q}")
    if len(hits) == 1:
        return str(hits[0]["_number"])
    print(f"{len(hits)} changes match {q}; pick a number:")
    print_change_list(hits)
    return None


# ---------------------------------------------------------------- commands

def cmd_query(g, args):
    changes = query_changes(g, args.query, args.n)
    if args.json:
        return emit_json(changes)
    if not changes:
        print("no results")
    print_change_list(changes)


def format_votes(label):
    votes = [(v.get("value", 0), who(v)) for v in label.get("all", []) if v.get("value")]
    votes.sort(key=lambda x: -x[0])
    return ", ".join(f"{val:+d} {name}" for val, name in votes)


def label_summary(label):
    for key in ("rejected", "approved", "disliked", "recommended"):
        if key in label:
            return f"{key} by {who(label[key])}"
    return ""


def print_files(files):
    for path, info in sorted(files.items()):
        if path == "/COMMIT_MSG":
            continue
        status = info.get("status", "M")
        old = f" (from {info['old_path']})" if info.get("old_path") else ""
        bin_ = " [binary]" if info.get("binary") else ""
        print(f"  {status} {path}{old}  +{info.get('lines_inserted', 0)} "
              f"-{info.get('lines_deleted', 0)}{bin_}")


def cmd_change(g, args):
    number = resolve_number(g, args.ref)
    if number is None:
        return
    c = g.get(change_path(number), [("o", o) for o in (
        "DETAILED_ACCOUNTS", "DETAILED_LABELS", "MESSAGES", "CURRENT_REVISION",
        "CURRENT_FILES", "CURRENT_COMMIT")])
    if args.json:
        return emit_json(c)
    rev = c.get("revisions", {}).get(c.get("current_revision"), {})
    print(f"Change {c['_number']}: {c['subject']}")
    print(f"  https://{g.host}/c/{c['project']}/+/{c['_number']}")
    print(f"  project: {c['project']}  branch: {c['branch']}  status: {c['status']}"
          + ("  [WIP]" if c.get("work_in_progress") else ""))
    print(f"  owner: {who(c.get('owner'))}")
    print(f"  created: {minute(c.get('created'))}  updated: {minute(c.get('updated'))}"
          + (f"  submitted: {minute(c['submitted'])} by {who(c.get('submitter'))}"
             if c.get("submitted") else ""))
    print(f"  patchset: {rev.get('_number', '?')} ({c.get('current_revision', '')[:12]})  "
          f"+{c.get('insertions', 0)} -{c.get('deletions', 0)}  "
          f"comments: {c.get('total_comment_count', 0)} "
          f"({c.get('unresolved_comment_count', 0)} unresolved)")
    print(f"  Change-Id: {c.get('change_id')}")
    if c.get("topic"):
        print(f"  topic: {c['topic']}")

    print("\nLabels:")
    for name, label in sorted(c.get("labels", {}).items()):
        votes = format_votes(label)
        summary = label_summary(label)
        print(f"  {name}: {votes or 'no votes'}" + (f"  [{summary}]" if summary else ""))

    commit = rev.get("commit", {})
    if commit.get("message"):
        print("\nCommit message:")
        print(indent(commit["message"].rstrip(), "  "))

    print("\nFiles:")
    print_files(rev.get("files", {}))

    names = account_names(c)
    print("\nMessages:")
    started = []

    def flush_started():
        if started:
            print(f"- {minute(started[0]['date'])} PS{started[0].get('_revision_number', '?')} "
                  f"{who(started[0].get('author'))}: Build Started x{len(started)}")
            started.clear()

    for m in c.get("messages", []):
        text = ACCOUNT_PLACEHOLDER_RE.sub(lambda x: names.get(x.group(1), x.group(0)),
                                          m.get("message", ""))
        if BUILD_STARTED_RE.match(text):
            started.append(m)
            continue
        flush_started()
        ps = m.get("_revision_number", "?")
        print(f"- {minute(m.get('date'))} PS{ps} {who(m.get('author') or m.get('real_author'))}:")
        print(indent(clip(text, args.max_msg), "    "))
    flush_started()


def account_names(obj, names=None):
    """Collect _account_id -> name from every account object in a response."""
    names = {} if names is None else names
    if isinstance(obj, dict):
        if "_account_id" in obj and obj.get("name"):
            names[str(obj["_account_id"])] = obj["name"]
        for v in obj.values():
            account_names(v, names)
    elif isinstance(obj, list):
        for v in obj:
            account_names(v, names)
    return names


def cmd_comments(g, args):
    data = g.get(change_path(args.number, "comments"))
    if args.json:
        return emit_json(data)
    total = sum(len(v) for v in data.values())
    print(f"Change {args.number}: {total} comments in {len(data)} files")

    def file_key(path):
        return (0 if path == "/PATCHSET_LEVEL" else 1 if path == "/COMMIT_MSG" else 2, path)

    unresolved_threads = 0
    for path in sorted(data, key=file_key):
        comments = data[path]
        by_id = {cm["id"]: cm for cm in comments}
        children = {}
        roots = []
        for cm in comments:
            parent = cm.get("in_reply_to")
            if parent in by_id:
                children.setdefault(parent, []).append(cm)
            else:
                roots.append(cm)
        for lst in children.values():
            lst.sort(key=lambda cm: cm.get("updated", ""))
        roots.sort(key=lambda cm: (cm.get("patch_set", 0), cm.get("line", 0), cm.get("updated", "")))

        def last_of(cm):
            kids = children.get(cm["id"])
            return last_of(kids[-1]) if kids else cm

        lines = []

        def render(cm, depth):
            loc = f"L{cm['line']}" if "line" in cm else "file"
            state = "UNRESOLVED" if cm.get("unresolved") else "resolved"
            head = f"PS{cm.get('patch_set', '?')} {loc} [{state}] " if depth == 0 \
                else f"↳ [{state}] "
            pad = "  " + "    " * depth
            lines.append(f"{pad}{head}{who(cm.get('author'))} {minute(cm.get('updated'))}:")
            lines.append(indent(cm.get("message", "").rstrip(), pad + "    "))
            for kid in children.get(cm["id"], []):
                render(kid, depth + 1)

        for root in roots:
            unresolved = bool(last_of(root).get("unresolved"))
            unresolved_threads += unresolved
            if args.unresolved and not unresolved:
                continue
            render(root, 0)
        if lines:
            print(f"\n{path}")
            print("\n".join(lines))
    print(f"\n{unresolved_threads} unresolved thread(s)")


def cmd_files(g, args):
    rev = str(args.ps) if args.ps else "current"
    files = g.get(change_path(args.number, "revisions", rev, "files"))
    if args.json:
        return emit_json(files)
    print(f"Change {args.number} patchset {rev}:")
    print_files(files)


def render_diff(d, context, path):
    out = list(d.get("diff_header", [])) or [f"--- a/{path}", f"+++ b/{path}"]
    if d.get("binary"):
        out.append("(binary file, not shown)")
        return out
    # Flatten into (tag, text) with positions so context can be trimmed.
    rows = []
    for chunk in d.get("content", []):
        if "skip" in chunk:
            rows.append(("skip", chunk["skip"]))
            continue
        for line in chunk.get("ab", []):
            rows.append((" ", line))
        for line in chunk.get("a", []):
            rows.append(("-", line))
        for line in chunk.get("b", []):
            rows.append(("+", line))
    changed = [i for i, (tag, _) in enumerate(rows) if tag in ("+", "-")]
    keep = set()
    for i in changed:
        keep.update(range(max(0, i - context), min(len(rows), i + context + 1)))
    a_no = b_no = 1
    hunk = None  # [a_start, b_start, a_count, b_count, lines]

    def close():
        if hunk:
            a0, b0, na, nb, lines = hunk
            out.append(f"@@ -{a0},{na} +{b0},{nb} @@")
            out.extend(lines)

    for i, (tag, text) in enumerate(rows):
        if tag == "skip":
            a_no += text
            b_no += text
            close()
            hunk = None
            continue
        if i in keep:
            if hunk is None:
                hunk = [a_no, b_no, 0, 0, []]
            hunk[4].append(tag + text)
            hunk[2] += tag in " -"
            hunk[3] += tag in " +"
        elif hunk is not None:
            close()
            hunk = None
        a_no += tag in " -"
        b_no += tag in " +"
    close()
    return out


def cmd_diff(g, args):
    rev = str(args.ps) if args.ps else "current"
    params = [("base", args.base)] if args.base else []
    if args.file:
        paths = [args.file]
    else:
        files = g.get(change_path(args.number, "revisions", rev, "files"), params)
        paths = sorted(p for p in files if p != "/COMMIT_MSG")
    out = []
    results = {}
    for path in paths:
        d = g.get(change_path(args.number, "revisions", rev, "files",
                              urllib.parse.quote(path, safe=""), "diff"), params)
        if args.json:
            results[path] = d
            continue
        out.extend(render_diff(d, args.context, path))
        out.append("")
    if args.json:
        return emit_json(results)
    if not paths:
        print("(no differences)")
    elif len(out) > args.max_lines:
        print("\n".join(out[:args.max_lines]))
        print(f"…[truncated: {len(out) - args.max_lines} more lines; "
              "pass a file or raise --max-lines]")
    else:
        print("\n".join(out).rstrip())


def cmd_related(g, args):
    related = g.get(change_path(args.number, "revisions", "current", "related"))
    c = g.get(change_path(args.number))
    others = [x for x in query_changes(g, f"change:{c['change_id']}", 50)
              if str(x["_number"]) != args.number]
    if args.json:
        return emit_json({"related": related.get("changes", []), "same_change_id": others})
    series = related.get("changes", [])
    print(f"Change {args.number}: {c['subject']}")
    print(f"\nSeries ({len(series)} related, newest first):")
    if not series:
        print("  (none)")
    for r in series:
        mark = "*" if str(r.get("_change_number")) == args.number else " "
        ps = r.get("_revision_number", "?")
        cur = r.get("_current_revision_number", ps)
        stale = f" (current PS{cur})" if cur != ps else ""
        subj = r.get("commit", {}).get("subject", "")
        print(f" {mark}{r.get('_change_number', '?'):>7} PS{ps}{stale} {r.get('status', ''):<8} {subj}")
    print(f"\nSame Change-Id on other branches ({len(others)}):")
    if not others:
        print("  (none)")
    print_change_list(others)


# --------------------------------------------------------------------- main

def build_parser():
    # --host/--json are accepted before or after the subcommand.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--host", type=host_name, default=argparse.SUPPRESS,
                        help="Gerrit hostname (default: $GERRIT_HOST)")
    common.add_argument("--json", action="store_true", default=argparse.SUPPRESS,
                        help="emit trimmed JSON instead of text")
    p = argparse.ArgumentParser(prog="gerrit-query.py", parents=[common],
                                description="Read-only Gerrit REST client (GET only).")
    sub = p.add_subparsers(dest="cmd", required=True, metavar="COMMAND")
    sub_add = sub.add_parser
    sub.add_parser = lambda *a, **kw: sub_add(*a, parents=[common], **kw)

    s = sub.add_parser("change", help="show a change: metadata, labels, messages, files")
    s.add_argument("ref", type=change_ref, help="change number, ticket key (MB-123) or Change-Id")
    s.add_argument("--max-msg", type=bounded_int(0, 1000000), default=800,
                   help="truncate each message to N chars (0 = no limit; default 800)")
    s.set_defaults(func=cmd_change)

    s = sub.add_parser("comments", help="inline comments grouped by file, threaded")
    s.add_argument("number", type=change_number)
    s.add_argument("--unresolved", action="store_true", help="only unresolved threads")
    s.set_defaults(func=cmd_comments)

    s = sub.add_parser("query", help="search changes with a Gerrit query")
    s.add_argument("query", type=query_string)
    s.add_argument("-n", type=bounded_int(1, 100), default=25, help="max results (default 25)")
    s.set_defaults(func=cmd_query)

    s = sub.add_parser("files", help="files changed in a patchset")
    s.add_argument("number", type=change_number)
    s.add_argument("--ps", type=bounded_int(1, 9999), help="patchset (default current)")
    s.set_defaults(func=cmd_files)

    s = sub.add_parser("diff", help="unified diff of a change or one file")
    s.add_argument("number", type=change_number)
    s.add_argument("file", nargs="?", type=file_path, help="restrict to one file")
    s.add_argument("--ps", type=bounded_int(1, 9999), help="patchset (default current)")
    s.add_argument("--base", type=bounded_int(1, 9999), help="diff against this patchset")
    s.add_argument("--context", type=bounded_int(0, 1000), default=3)
    s.add_argument("--max-lines", type=bounded_int(1, 1000000), default=2000)
    s.set_defaults(func=cmd_diff)

    s = sub.add_parser("related", help="series (dependency chain) and cherry-picks")
    s.add_argument("number", type=change_number)
    s.set_defaults(func=cmd_related)
    return p


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    args.json = getattr(args, "json", False)
    host = getattr(args, "host", None)
    if host is None:
        env = os.environ.get("GERRIT_HOST", "")
        if not env:
            parser.error("no Gerrit host: set GERRIT_HOST or pass --host")
        try:
            host = host_name(env)
        except argparse.ArgumentTypeError as e:
            parser.error(f"GERRIT_HOST: {e}")
    try:
        args.func(Gerrit(host), args)
    except UsageError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    except BrokenPipeError:
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
