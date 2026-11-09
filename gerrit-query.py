#!/usr/bin/env python3
"""Read-only Gerrit CLI.

Only issues anonymous HTTPS GET requests to a Gerrit REST API. Every argument
is validated against a conservative pattern before it becomes part of a URL;
there is no way to pass a raw URL, an ssh command or a write operation.

Host: --host <hostname> or the GERRIT_HOST environment variable.
"""
import argparse
import http.client
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


class NotFound(UsageError):
    pass


class BadStatus(UsageError):
    """An HTTP error status other than 404."""
    def __init__(self, message, code):
        super().__init__(message)
        self.code = code


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


def date_arg(value):
    """YYYY-MM-DD or YYYY-MM-DD HH:MM, compared as a prefix of Gerrit's UTC timestamps."""
    if not re.match(r"^\d{4}-\d{2}-\d{2}(?:[ T]\d{2}:\d{2})?$", value):
        raise argparse.ArgumentTypeError(
            f"invalid date {value!r}: expected YYYY-MM-DD or 'YYYY-MM-DD HH:MM'")
    return value.replace("T", " ")


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
                raise NotFound(self._explain_404(path, dict(params or [])))
            raise BadStatus(f"HTTP {e.code} for {path}", e.code)
        except urllib.error.URLError as e:
            raise UsageError(f"request failed: {e.reason}")
        except (OSError, http.client.HTTPException) as e:
            # Read timeouts, resets and TLS errors are not wrapped in URLError.
            raise UsageError(f"request failed: {e.__class__.__name__}: {e}")
        except UnicodeDecodeError:
            raise UsageError(f"response for {path} is not UTF-8")
        try:
            return self._decode(body)
        except json.JSONDecodeError:
            raise UsageError(f"response for {path} is not JSON")

    def _exists(self, path):
        """True on success, False on 404. Any other outcome raises, so that
        _explain_404 falls back to the plain message rather than guess."""
        req = urllib.request.Request(f"https://{self.host}{path}", method="GET",
                                     headers={"Accept": "application/json"})
        try:
            with self.opener.open(req, timeout=TIMEOUT):
                return True
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return False
            raise

    def _explain_404(self, path, params):
        """Turn a 404 under /changes/N/... into which of change, patchset or file is missing.

        Gerrit answers 404 for each of these (and for changes the anonymous
        user can't see), so probe from the top down. Runs only on the error path,
        and falls back to the plain message if anything goes wrong."""
        try:
            return self._probe_404(path, params)
        except Exception:
            return f"not found: {path}"

    def _probe_404(self, path, params):
        m = re.match(r"^/changes/(\d+)(?:/revisions/([^/]+)(?:/files/([^/]+))?)?", path)
        if not m:
            return f"not found: {path}"
        number, rev, file_ = m.groups()
        if not self._exists(f"/changes/{number}"):
            return f"change {number} not found (or not visible without logging in)"
        if rev and rev != "current" and not self._exists(f"/changes/{number}/revisions/{rev}/files"):
            return f"change {number} has no patchset {rev}"
        base = params.get("base")
        if base and not self._exists(f"/changes/{number}/revisions/{base}/files"):
            return f"change {number} has no patchset {base} (--base)"
        if file_:
            where = "the current patchset" if rev == "current" else f"patchset {rev}"
            return f"file {urllib.parse.unquote(file_)} not found in {where} of change {number}"
        return f"not found: {path}"

    @staticmethod
    def _decode(body):
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


def query_changes(g, q, limit):
    return g.get("/changes/", [("q", q), ("n", limit), ("o", "DETAILED_ACCOUNTS"),
                               ("o", "DETAILED_LABELS"), ("o", "CURRENT_REVISION")])


def current_ps(c):
    return c.get("revisions", {}).get(c.get("current_revision"), {}).get("_number")


def change_line(c, prefix=""):
    """One line per change: enough to triage without opening each one."""
    status = c["status"] + ("/WIP" if c.get("work_in_progress") else "")
    ps = current_ps(c)
    unresolved = c.get("unresolved_comment_count", 0)
    topic = f"  topic:{c['topic']}" if c.get("topic") else ""
    return (f"{prefix}{c['_number']:>7}  {status:<9} {'PS' + str(ps) if ps else '':<5} "
            f"{short_labels(c):<10} {str(unresolved) + ' unresolved':<13} "
            f"{day(c.get('updated'))}  {who(c.get('owner')):<18.18} "
            f"{c['project']}/{c['branch']}  {c['subject']}{topic}")


def print_change_list(changes, prefix=""):
    for c in changes:
        print(change_line(c, prefix))
    if changes and changes[-1].get("_more_changes"):
        print(f"{prefix}(more results available; raise -n)")


LABEL_ABBREV = {"Verified": "V", "Code-Review": "CR"}


def label_score(label):
    """The deciding vote of a label: the lowest if any is negative, else the highest."""
    values = [v.get("value", 0) for v in label.get("all", []) if v.get("value")]
    if not values:
        return 0
    return min(values) if min(values) < 0 else max(values)


def short_labels(c):
    parts = []
    for name, abbrev in LABEL_ABBREV.items():
        if name in c.get("labels", {}):
            score = label_score(c["labels"][name])
            parts.append(f"{abbrev}{score:+d}" if score else f"{abbrev} -")
    return " ".join(parts)


def topic_query(topic):
    """A topic: query term, or None if the topic can't be quoted safely."""
    if topic and re.match(r'^[A-Za-z0-9_.:/+\-~@ ]{1,200}$', topic):
        return f'topic:"{topic}"'
    return None


def sibling_changes(g, c):
    """Other changes sharing this change's Change-Id (any project or branch) or topic."""
    terms = [f"change:{c['change_id']}"]
    tq = topic_query(c.get("topic"))
    if tq:
        terms.append(tq)
    hits = query_changes(g, " OR ".join(terms), 100)
    more = bool(hits) and hits[-1].get("_more_changes")
    hits = [x for x in hits if x["_number"] != c["_number"]]
    if hits and more:
        hits[-1]["_more_changes"] = True
    return hits


def threads_of(lst):
    """Group one file's comments into threads: root comment id -> its comments."""
    by_id = {cm["id"]: cm for cm in lst}

    def root_of(cm):
        seen = set()
        while cm.get("in_reply_to") in by_id and cm["id"] not in seen:
            seen.add(cm["id"])
            cm = by_id[cm["in_reply_to"]]
        return cm["id"]

    threads = {}
    for cm in lst:
        threads.setdefault(root_of(cm), []).append(cm)
    return threads


def newest(comments):
    return max(comments, key=lambda x: x.get("updated", ""))


def thread_states(comments):
    """Map comment id -> True if that comment's thread is unresolved.

    A thread's state is the state of its newest comment."""
    states = {}
    for lst in comments.values():
        for members in threads_of(lst).values():
            unresolved = bool(newest(members).get("unresolved"))
            for cm in members:
                states[cm["id"]] = unresolved
    return states


def code_lines(cm, prefix, limit=6):
    """The source lines a comment is attached to (from enable-context)."""
    ctx = cm.get("context_lines") or []
    out = [f"{prefix}| {x.get('line_number', '?'):>5}  {x.get('context_line', '')}"
           for x in ctx[:limit]]
    if len(ctx) > limit:
        out.append(f"{prefix}| …[+{len(ctx) - limit} lines]")
    return out


def get_comments(g, number):
    """Comments with the code lines they're attached to, where the server supports it."""
    try:
        return g.get(change_path(number, "comments"),
                     [("enable-context", "true"), ("context-padding", "0")])
    except BadStatus as e:
        # Older Gerrit rejects the unknown option with 400; retry without the context.
        if e.code != 400:
            raise
        return g.get(change_path(number, "comments"))


def print_siblings(c, siblings, series=(), skip_empty=False):
    """series: change numbers already listed as the series; left out of the topic list.

    skip_empty leaves out a section with nothing in it (for change, where
    siblings are a side note rather than what was asked for)."""
    same_id = [x for x in siblings if x["change_id"] == c["change_id"]]
    same_topic = [x for x in siblings
                  if x["change_id"] != c["change_id"] and x["_number"] not in series]
    sections = [("Same Change-Id in any project or branch", same_id)]
    if c.get("topic"):
        outside = " outside the series" if series else ""
        sections.append((f"Same topic {c['topic']}{outside}", same_topic))
    for title, changes in sections:
        if not changes and skip_empty:
            continue
        print(f"\n{title} ({len(changes)}):")
        if changes:
            print_change_list(changes, "  ")
        else:
            print("  (none)")


def resolve_number(g, ref, limit):
    """Map a change ref to a single change number, or print candidates and return None."""
    if NUMBER_RE.match(ref):
        return ref
    q = f"change:{ref}" if CHANGE_ID_RE.match(ref) else f"message:{ref}"
    hits = query_changes(g, q, limit)
    if not hits:
        raise UsageError(f"no changes match {q}")
    if len(hits) == 1:
        return str(hits[0]["_number"])
    count = f"first {len(hits)}" if hits[-1].get("_more_changes") else str(len(hits))
    print(f"{count} changes match {q}; pick a number:")
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


def parent_note(g, c, sha):
    """Say whether a parent commit is the branch tip, another open change, or older."""
    if c.get("status") != "NEW":
        return ""
    hits = query_changes(g, f"commit:{sha}", 1) if re.match(r"^[0-9a-f]{40}$", sha) else []
    if not hits:
        return branch_tip_note(g, c, sha)
    h = hits[0]
    if h["status"] == "MERGED":
        return f"  [merged as change {h['_number']}]" + branch_tip_note(g, c, sha)
    # commit: matches any patchset of h, not just its current one.
    if h.get("current_revision") and h["current_revision"] != sha:
        return (f"  [older patchset of change {h['_number']}, {h['status']}; "
                f"current is PS{current_ps(h)}: needs rebase]")
    return f"  [change {h['_number']}, {h['status']}]"


def branch_tip_note(g, c, sha):
    try:
        tip = g.get("/projects/" + urllib.parse.quote(c["project"], safe="")
                    + "/branches/" + urllib.parse.quote(c["branch"], safe=""))
    except UsageError:
        return ""
    rev = tip.get("revision", "")
    return "  [tip of " + c["branch"] + "]" if rev == sha \
        else f"  [{c['branch']} tip is now {rev[:12]}]"


def activity_cutoff(c, number, args):
    """Earliest timestamp to show for --since-ps/--after ("" = show everything).

    --since-ps cuts off by when patchset N was uploaded, not by each item's
    patchset tag: a reply to an older patchset's comments, posted alongside or
    after the new upload, is still tagged with the older patchset.
    c must have been fetched with ALL_REVISIONS when --since-ps is given."""
    cutoff = args.after or ""
    if args.since_ps:
        created = [r.get("created", "") for r in c.get("revisions", {}).values()
                   if r.get("_number") == args.since_ps]
        if not created:
            raise UsageError(f"change {number} has no patchset {args.since_ps}")
        cutoff = max(cutoff, created[0])
    return cutoff


def add_since_args(s, what):
    s.add_argument("--since-ps", type=bounded_int(1, 9999),
                   help=f"only show {what} posted since patchset N was uploaded, "
                        "including late replies on older patchsets")
    s.add_argument("--after", type=date_arg,
                   help=f"only show {what} posted at or after this UTC time "
                        "(YYYY-MM-DD or 'YYYY-MM-DD HH:MM')")


def cmd_change(g, args):
    number = resolve_number(g, args.ref, args.n)
    if number is None:
        return
    c = g.get(change_path(number), [("o", o) for o in (
        "DETAILED_ACCOUNTS", "DETAILED_LABELS", "MESSAGES", "CURRENT_REVISION",
        "CURRENT_FILES", "CURRENT_COMMIT", *(["ALL_REVISIONS"] if args.since_ps else []))])
    cutoff = activity_cutoff(c, number, args)
    messages = c.get("messages", [])
    shown = [m for m in messages if m.get("date", "") >= cutoff]
    # A reply's text lives in its comments, not in the change message ("(1 comment)").
    comments = {} if args.no_comments else get_comments(g, number)
    siblings = sibling_changes(g, c)
    if args.json:
        if cutoff:
            # Same filter as the text output: the shown messages and their comments.
            c["messages"] = shown
            ids = {m.get("id") for m in shown}
            comments = {path: kept for path, lst in comments.items()
                        if (kept := [cm for cm in lst if cm.get("change_message_id") in ids])}
        if not args.no_comments:
            c["comments"] = comments
        c["siblings"] = siblings
        return emit_json(c)
    states = thread_states(comments)
    by_message = {}
    for path, lst in comments.items():
        for cm in lst:
            by_message.setdefault(cm.get("change_message_id"), []).append((path, cm))
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
    if rev.get("ref"):
        print(f"  ref: {rev['ref']}")
    for parent in rev.get("commit", {}).get("parents", []):
        sha = parent.get("commit", "")
        print(f"  parent: {sha[:12]} {parent.get('subject', '')}{parent_note(g, c, sha)}")

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

    print_siblings(c, siblings, skip_empty=True)

    names = account_names(c)
    hidden = len(messages) - len(shown)
    print("\nMessages:" + (f" ({hidden} earlier hidden by --since-ps/--after)" if hidden else ""))
    started = []

    def flush_started():
        if started:
            print(f"- {minute(started[0]['date'])} PS{started[0].get('_revision_number', '?')} "
                  f"{who(started[0].get('author'))}: Build Started x{len(started)}")
            started.clear()

    for m in shown:
        text = ACCOUNT_PLACEHOLDER_RE.sub(lambda x: names.get(x.group(1), x.group(0)),
                                          m.get("message", ""))
        if BUILD_STARTED_RE.match(text):
            started.append(m)
            continue
        flush_started()
        ps = m.get("_revision_number", "?")
        print(f"- {minute(m.get('date'))} PS{ps} {who(m.get('author') or m.get('real_author'))}:")
        print(indent(clip(text, args.max_msg), "    "))
        for path, cm in sorted(by_message.get(m.get("id"), []),
                               key=lambda pc: (pc[0] != "/PATCHSET_LEVEL", pc[0], pc[1].get("line", 0))):
            where = "patchset" if path == "/PATCHSET_LEVEL" else \
                f"{path}:{cm['line']}" if "line" in cm else path
            state = "thread UNRESOLVED" if states.get(cm["id"]) else "thread resolved"
            ps = f"PS{cm['patch_set']} " if cm.get("patch_set") else ""
            print(f"    comment {ps}{where} [{state}]"
                  + (" (reply)" if cm.get("in_reply_to") else "") + ":")
            if not cm.get("in_reply_to"):
                for line in code_lines(cm, "      "):
                    print(line)
            print(indent(clip(cm.get("message", "").rstrip(), args.max_msg), "      "))
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
    data = get_comments(g, args.number)
    cutoff = ""
    if args.since_ps or args.after:
        c = g.get(change_path(args.number), [("o", "ALL_REVISIONS")]) if args.since_ps else {}
        cutoff = activity_cutoff(c, args.number, args)
    if args.json:
        if cutoff:
            # Keep whole threads that have activity since the cutoff.
            data = {path: [cm for members in threads_of(lst).values()
                           if newest(members).get("updated", "") >= cutoff for cm in members]
                    for path, lst in data.items()}
            data = {p: lst for p, lst in data.items() if lst}
        return emit_json(data)
    total = sum(len(v) for v in data.values())
    print(f"Change {args.number}: {total} comments in {len(data)} files")
    if cutoff:
        print(f"Showing threads with a comment at or after {cutoff[:16]} UTC; "
              "comments from then on are marked (new).")

    def file_key(path):
        return (0 if path == "/PATCHSET_LEVEL" else 1 if path == "/COMMIT_MSG" else 2, path)

    unresolved_threads = shown = 0
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
            """Newest comment anywhere in the thread; it decides the thread's state."""
            return max([cm, *(last_of(kid) for kid in children.get(cm["id"], []))],
                       key=lambda c: c.get("updated", ""))

        lines = []

        def render(cm, depth):
            if depth == 0:
                # The root carries the thread's final state; replies keep their own flag.
                loc = f" L{cm['line']}" if "line" in cm else \
                    "" if path == "/PATCHSET_LEVEL" else " file"
                state = "thread UNRESOLVED" if last_of(cm).get("unresolved") else "thread resolved"
                head = f"PS{cm.get('patch_set', '?')}{loc} [{state}] "
            else:
                head = f"↳ [{'UNRESOLVED' if cm.get('unresolved') else 'resolved'}] "
            pad = "  " + "    " * depth
            new = " (new)" if cutoff and cm.get("updated", "") >= cutoff else ""
            lines.append(f"{pad}{head}{who(cm.get('author'))} {minute(cm.get('updated'))}{new}:")
            if depth == 0:
                lines.extend(code_lines(cm, pad + "    "))
            lines.append(indent(cm.get("message", "").rstrip(), pad + "    "))
            for kid in children.get(cm["id"], []):
                render(kid, depth + 1)

        for root in roots:
            unresolved = bool(last_of(root).get("unresolved"))
            unresolved_threads += unresolved
            if args.unresolved and not unresolved:
                continue
            if cutoff and last_of(root).get("updated", "") < cutoff:
                continue
            shown += 1
            render(root, 0)
        if lines:
            print("\npatchset-level" if path == "/PATCHSET_LEVEL" else f"\n{path}")
            print("\n".join(lines))
    if cutoff and not shown:
        print("\n(no threads with activity since then)")
    print(f"\n{unresolved_threads} unresolved thread(s) in the change"
          + (f"; {shown} shown" if cutoff or args.unresolved else ""))


def cmd_files(g, args):
    rev = str(args.ps) if args.ps else "current"
    files = g.get(change_path(args.number, "revisions", rev, "files"))
    if args.json:
        return emit_json(files)
    print(f"Change {args.number} patchset {rev}:")
    print_files(files)


def render_diff(d, context, path, numbers=True):
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
            if numbers:
                old = str(a_no) if tag in " -" else ""
                new = str(b_no) if tag in " +" else ""
                hunk[4].append(f"{old:>5} {new:>5} {tag}{text}")
            else:
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
        # Gerrit answers 200 with no file metadata for a path absent on both sides.
        if not d.get("meta_a") and not d.get("meta_b"):
            where = f"patchset {args.ps}" if args.ps else "the current patchset"
            base = f" or its base patchset {args.base}" if args.base else " or its parent"
            raise UsageError(f"file {path} not found in {where}{base} of change {args.number}")
        if args.json:
            results[path] = d
            continue
        out.extend(render_diff(d, args.context, path, not args.no_line_numbers))
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


def cmd_show(g, args):
    rev = str(args.ps) if args.ps else "current"
    # With Accept: application/json Gerrit returns the content as a JSON string.
    text = g.get(change_path(args.number, "revisions", rev, "files",
                             urllib.parse.quote(args.file, safe=""), "content"))
    if not isinstance(text, str):
        raise UsageError(f"unexpected response for {args.file}")
    if "\0" in text:
        raise UsageError(f"{args.file} looks binary; not shown")
    # Split on \n only, as Gerrit numbers lines; splitlines() also breaks on
    # \f, \x1c-\x1e, \x85 and \u2028, which would shift every later number.
    # A CRLF file keeps its \r on each line after split("\n"); drop it.
    lines = [line.rstrip("\r") for line in text.split("\n")]
    if lines[-1] == "":
        lines.pop()  # the newline ending the last line
    where = f"patchset {rev}" if args.ps else "current patchset"
    if not lines and not args.lines:
        if args.json:
            return emit_json({"file": args.file, "patchset": rev, "first_line": 1,
                              "total_lines": 0, "lines": []})
        print(f"Change {args.number} {where}: {args.file} (empty file)")
        return
    first, last = args.lines or (1, len(lines))
    if first > len(lines):
        raise UsageError(f"{args.file} has only {len(lines)} lines")
    wanted = min(last, len(lines))
    # The header names the lines actually printed, after the --max-lines cap.
    last = min(wanted, first + args.max_lines - 1)
    picked = lines[first - 1:last]
    if args.json:
        return emit_json({"file": args.file, "patchset": rev, "first_line": first,
                          "total_lines": len(lines), "lines": picked})
    print(f"Change {args.number} {where}: {args.file} "
          f"(lines {first}-{last} of {len(lines)})")
    for i, line in enumerate(picked, first):
        print(f"{i:>5}  {line}")
    if wanted > last:
        print(f"…[truncated: {wanted - last} more lines; "
              "use --lines A-B or raise --max-lines]")


def line_range(value):
    m = re.match(r"^(\d{1,7})-(\d{1,7})$", value)
    if not m or not 1 <= int(m.group(1)) <= int(m.group(2)):
        raise argparse.ArgumentTypeError(f"expected a line range like 40-80, got {value!r}")
    return int(m.group(1)), int(m.group(2))


def outdated_parent_note(series, i):
    """Flag a series entry that isn't built on its parent change's current patchset.

    series is newest first, so an entry's parent change is the next entry."""
    if i + 1 >= len(series):
        return ""
    parent = series[i + 1]
    if not parent.get("_change_number"):
        return ""
    parents = [p.get("commit") for p in series[i].get("commit", {}).get("parents", [])]
    listed = parent.get("commit", {}).get("commit")
    if listed in parents:
        # Built on the listed patchset; is that the parent's current one?
        if parent.get("_revision_number") == parent.get("_current_revision_number"):
            return ""
        on = f"PS{parent.get('_revision_number')}"
    else:
        on = "an older patchset"
    return (f"  [on {on} of {parent['_change_number']}, current is "
            f"PS{parent.get('_current_revision_number')}: needs rebase]")


def cmd_related(g, args):
    related = g.get(change_path(args.number, "revisions", "current", "related"))
    series = related.get("changes", [])
    # The related endpoint has no topics or votes; fetch them (and this change) in one query.
    numbers = {args.number, *(str(r["_change_number"]) for r in series if r.get("_change_number"))}
    info = {x["_number"]: x for x in query_changes(g, " OR ".join(f"change:{n}" for n in numbers), 100)}
    c = info.get(int(args.number))
    if c is None:
        raise UsageError(f"change {args.number} not found")
    siblings = sibling_changes(g, c)
    if args.json:
        return emit_json({"change": c, "related": series, "siblings": siblings})
    print(f"Change {args.number}: {c['subject']}")
    print(f"\nSeries ({len(series)} related, newest first):")
    if not series:
        print("  (none; this change only)")
        print(change_line(c, " *"))
    for i, r in enumerate(series):
        mark = " *" if str(r.get("_change_number")) == args.number else "  "
        ps = r.get("_revision_number", "?")
        cur = r.get("_current_revision_number", ps)
        # The list line shows the current patchset; say when the series uses an older one.
        stale = f"  [series has PS{ps}]" if cur != ps else ""
        x = info.get(r.get("_change_number"))
        if x:
            line = change_line(x, mark) + ("" if x.get("topic") else "  (no topic)")
        else:
            line = (f"{mark}{r.get('_change_number', '?'):>7}  {r.get('status', ''):<9} PS{ps}  "
                    f"{r.get('commit', {}).get('subject', '')}")
        print(f"{line}{stale}{outdated_parent_note(series, i)}")
    print_siblings(c, siblings, {x for x in info if x != c["_number"]})


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
    s.add_argument("-n", type=bounded_int(1, 100), default=50,
                   help="max candidates listed for a ticket key or Change-Id (default 50)")
    s.add_argument("--no-comments", action="store_true",
                   help="don't fetch or show review comments under the messages")
    s.add_argument("--max-msg", type=bounded_int(0, 1000000), default=800,
                   help="truncate each message to N chars (0 = no limit; default 800)")
    add_since_args(s, "messages (and their comments)")
    s.set_defaults(func=cmd_change)

    s = sub.add_parser("comments", help="inline comments grouped by file, threaded")
    s.add_argument("number", type=change_number)
    s.add_argument("--unresolved", action="store_true", help="only unresolved threads")
    add_since_args(s, "threads with a comment")
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
    s.add_argument("--no-line-numbers", action="store_true",
                   help="plain unified diff without the old/new line-number columns")
    s.set_defaults(func=cmd_diff)

    s = sub.add_parser("show", help="a file's full content at a patchset, with line numbers")
    s.add_argument("number", type=change_number)
    s.add_argument("file", type=file_path)
    s.add_argument("--ps", type=bounded_int(1, 9999), help="patchset (default current)")
    s.add_argument("--lines", type=line_range, help="only lines A-B, e.g. 40-80")
    s.add_argument("--max-lines", type=bounded_int(1, 1000000), default=2000)
    s.set_defaults(func=cmd_show)

    s = sub.add_parser("related", help="series, same Change-Id elsewhere, same topic")
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
