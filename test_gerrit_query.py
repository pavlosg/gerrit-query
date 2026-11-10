"""Offline tests for gerrit-query.py. Run from the repo root: python3 -m unittest

No network: commands run against FakeGerrit, which serves hand-written
responses shaped like Gerrit's REST API, and the HTTP layer is tested
through a fake urllib opener.
"""
import contextlib
import importlib
import io
import json
import unittest
import urllib.error

gq = importlib.import_module("gerrit-query")  # the hyphen rules out a plain import


# ------------------------------------------------------------------ helpers

class FakeGerrit:
    """Serves routes: path -> value, or callable(params) -> value. Unknown path -> 404."""
    host = "review.example.org"

    def __init__(self, routes):
        self.routes = routes
        self.calls = []

    def get(self, path, params=None):
        self.calls.append((path, params))
        if path not in self.routes:
            raise gq.NotFound(f"not found: {path}")
        route = self.routes[path]
        return route(dict(params or [])) if callable(route) else route


def run(g, *argv):
    """Run a command the way main() does, returning its stdout."""
    args = gq.build_parser().parse_args(list(argv))
    args.json = getattr(args, "json", False)
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        args.func(g, args)
    return out.getvalue()


ME = {"_account_id": 1, "name": "Alice"}
BOT = {"_account_id": 2, "name": "Build Bot"}
T3 = "2026-11-01 10:00:00.000000000"
T4 = "2026-11-02 15:03:38.000000000"


def comment(id_, when, message, unresolved, msg_id, reply_to=None, line=None, ps=3,
            context=None, side=None):
    cm = {"id": id_, "updated": when, "message": message, "unresolved": unresolved,
          "change_message_id": msg_id, "patch_set": ps, "author": ME}
    if reply_to:
        cm["in_reply_to"] = reply_to
    if line:
        cm["line"] = line
    if context:
        cm["context_lines"] = context
    if side:
        cm["side"] = side
    return cm


def change_fixture(status="MERGED", **extra):
    """A change on PS4 whose PS3 review got a late reply after PS4 was uploaded."""
    c = {
        "_number": 100, "subject": "MB-1: Do a thing", "project": "kv_engine",
        "branch": "master", "status": status, "owner": ME, "change_id": "I" + "a" * 40,
        "created": T3, "updated": T4, "current_revision": "sha4",
        "insertions": 1, "deletions": 0, "total_comment_count": 2,
        "unresolved_comment_count": 0, "labels": {},
        "revisions": {
            "sha3": {"_number": 3, "created": T3},
            "sha4": {"_number": 4, "created": T4, "ref": "refs/changes/00/100/4",
                     "commit": {"message": "MB-1: Do a thing\n",
                                "parents": [{"commit": "f" * 40, "subject": "Parent"}]},
                     "files": {}},
        },
        "messages": [
            {"id": "m1", "date": "2026-11-02 11:00:00.000000000", "_revision_number": 3,
             "author": ME, "message": "Patch Set 3:\n\n(1 comment)"},
            {"id": "m2", "date": T4, "_revision_number": 4, "author": ME,
             "message": "Uploaded patch set 4."},
            {"id": "b1", "date": "2026-11-02 15:03:50.000000000", "_revision_number": 4,
             "author": BOT, "message": "Patch Set 4:\n\nBuild Started https://cv/1 (1/2)"},
            {"id": "b2", "date": "2026-11-02 15:03:53.000000000", "_revision_number": 4,
             "author": BOT, "message": "Patch Set 4:\n\nBuild Started https://cv/2 (2/2)"},
            # Posted after PS4 was uploaded, but Gerrit tags it with PS3.
            {"id": "m3", "date": "2026-11-02 15:03:45.000000000", "_revision_number": 3,
             "author": ME, "message": "Patch Set 3:\n\n(1 comment)"},
        ],
    }
    c.update(extra)
    return c


COMMENTS = {
    "src/a.cc": [
        comment("c1", "2026-11-02 11:00:00.000000000", "Is this needed?", True, "m1",
                line=51, context=[{"line_number": 51, "context_line": "template <typename U = T>"}]),
        comment("c2", "2026-11-02 15:03:45.000000000", "Done", False, "m3", reply_to="c1",
                line=51),
    ],
}


# A thread on the old side of the diff (shape seen on review.couchbase.org 257569).
BASE_SIDE_COMMENTS = {
    "src/a.cc": [
        comment("c1", "2026-11-02 11:00:00.000000000", "Removed stat?", True, "m1",
                line=51, side="PARENT",
                context=[{"line_number": 51, "context_line": '"ep_removed_stat",'}]),
        comment("c2", "2026-11-02 15:03:45.000000000", "Done", False, "m3", reply_to="c1",
                line=51, side="PARENT"),
    ],
}


def change_routes(c=None, comments=COMMENTS):
    c = c or change_fixture()
    return {
        "/changes/100": c,
        "/changes/100/comments": comments,
        "/changes/": lambda p: [],  # sibling and parent queries
    }


# --------------------------------------------------------------- validation

class ValidatorTest(unittest.TestCase):
    def test_change_ref(self):
        self.assertEqual(gq.change_ref("257332"), "257332")
        self.assertEqual(gq.change_ref("mb-123"), "MB-123")
        self.assertEqual(gq.change_ref("I" + "0" * 40), "I" + "0" * 40)
        for bad in ("1; rm -rf", "MB-", "I123", ""):
            with self.assertRaises(Exception, msg=bad):
                gq.change_ref(bad)

    def test_file_path(self):
        self.assertEqual(gq.file_path("engines/ep/src/a.h"), "engines/ep/src/a.h")
        for ok in ("docs/a b.md", "a~1", "100%.txt", "c#/x.cs", "a,b"):
            self.assertEqual(gq.file_path(ok), ok)
        for bad in ("../x", "/etc/passwd", "a/../b", "a\nb", "a\x00b", "",
                    "a" + chr(0x9b) + "b", "a" + chr(0x202e) + "b", "a" + chr(0x2066) + "b"):
            with self.assertRaises(Exception, msg=bad):
                gq.file_path(bad)

    def test_comment_file(self):
        self.assertEqual(gq.comment_file("/PATCHSET_LEVEL"), "/PATCHSET_LEVEL")
        self.assertEqual(gq.comment_file("src/a.cc"), "src/a.cc")
        with self.assertRaises(Exception):
            gq.comment_file("/etc/passwd")

    def test_date_arg(self):
        self.assertEqual(gq.date_arg("2026-11-05"), "2026-11-05")
        self.assertEqual(gq.date_arg("2026-11-05T10:00"), "2026-11-05 10:00")
        with self.assertRaises(Exception):
            gq.date_arg("5nov")

    def test_line_range(self):
        self.assertEqual(gq.line_range("40-80"), (40, 80))
        for bad in ("80-40", "0-3", "40", "a-b"):
            with self.assertRaises(Exception, msg=bad):
                gq.line_range(bad)

    def test_query_string(self):
        gq.query_string('project:kv_engine status:open owner:"A B"')
        for bad in ("a;b", "a|b", "a`b`", "x" * 301):
            with self.assertRaises(Exception, msg=bad[:10]):
                gq.query_string(bad)


# ---------------------------------------------------------------------- diff

class RenderDiffTest(unittest.TestCase):
    DIFF = {
        "diff_header": ["--- a/f", "+++ b/f"],
        "meta_a": {}, "meta_b": {},
        "content": [
            {"ab": ["a", "b", "c", "d"]},       # old 1-4, new 1-4
            {"a": ["old"], "b": ["new1", "new2"]},  # old 5; new 5-6
            {"ab": ["e", "f", "g", "h", "i"]},  # old 6-10, new 7-11
            {"skip": 100},                      # old 11-110, new 12-111
            {"ab": ["x"]},                      # old 111, new 112
            {"b": ["added"]},                   # new 113
        ],
    }

    def test_hunks_and_line_numbers(self):
        out = gq.render_diff(self.DIFF, 1, "f")
        self.assertEqual(out, [
            "--- a/f", "+++ b/f",
            "@@ -4,3 +4,4 @@",
            "    4     4  d",
            "    5       -old",
            "          5 +new1",
            "          6 +new2",
            "    6     7  e",
            "@@ -111,1 +112,2 @@",
            "  111   112  x",
            "        113 +added",
        ])

    def test_plain_patch(self):
        out = gq.render_diff(self.DIFF, 1, "f", numbers=False)
        self.assertEqual(out[2:8], ["@@ -4,3 +4,4 @@", " d", "-old", "+new1", "+new2", " e"])

    def test_binary(self):
        self.assertEqual(gq.render_diff({"binary": True}, 3, "img.png")[-1],
                         "(binary file, not shown)")

    def test_missing_file_is_an_error(self):
        g = FakeGerrit({"/changes/100/revisions/current/files/nope.h/diff":
                        {"change_type": "MODIFIED", "content": []},
                        "/changes/100": {"current_revision": "abc",
                                         "revisions": {"abc": {"_number": 3}}}})
        with self.assertRaisesRegex(gq.UsageError,
                                    "file nope.h not found in patchset 3 or its parent of change 100"):
            run(g, "diff", "100", "nope.h")


# ------------------------------------------------------------------- threads

class ThreadTest(unittest.TestCase):
    def test_state_comes_from_newest_comment(self):
        states = gq.thread_states({
            "f": [comment("r", "2026-01-01", "q", True, "m"),
                  comment("x", "2026-01-02", "done", False, "m", reply_to="r"),
                  comment("lone", "2026-01-01", "q2", True, "m")],
        })
        self.assertEqual(states, {"r": False, "x": False, "lone": True})

    def test_reply_cycle_terminates(self):
        lst = [comment("a", "1", "", True, "m", reply_to="b"),
               comment("b", "2", "", False, "m", reply_to="a")]
        self.assertEqual(sum(len(v) for v in gq.threads_of(lst).values()), 2)


# ------------------------------------------------------------------- related

class OutdatedParentTest(unittest.TestCase):
    def entry(self, number, commit, parent=None, ps=1, cur=1):
        e = {"_change_number": number, "_revision_number": ps,
             "_current_revision_number": cur, "commit": {"commit": commit}}
        if parent:
            e["commit"]["parents"] = [{"commit": parent}]
        return e

    def test_on_listed_but_outdated_patchset(self):
        series = [self.entry(2, "c2", parent="p5"), self.entry(1, "p5", ps=5, cur=6)]
        self.assertIn("on PS5 of 1, current is PS6", gq.outdated_parent_note(series, 0))

    def test_parent_moved_on_since(self):
        # Seen from the parent: it is listed at PS6, the child sits on PS5.
        series = [self.entry(2, "c2", parent="p5"), self.entry(1, "p6", ps=6, cur=6)]
        self.assertIn("on an older patchset of 1, current is PS6",
                      gq.outdated_parent_note(series, 0))

    def test_up_to_date_and_last_entry(self):
        series = [self.entry(2, "c2", parent="p6"), self.entry(1, "p6", ps=6, cur=6)]
        self.assertEqual(gq.outdated_parent_note(series, 0), "")
        self.assertEqual(gq.outdated_parent_note(series, 1), "")

    def test_related_output(self):
        def query(p):
            if "topic:" in p["q"]:  # siblings: one in the series, one outside
                return [{"_number": 1, "change_id": "Ione", "status": "NEW", "project": "kv",
                         "branch": "master", "subject": "Parent", "topic": "t"},
                        {"_number": 3, "change_id": "Ithree", "status": "NEW", "project": "platform",
                         "branch": "master", "subject": "Other repo", "topic": "t"}]
            return [{"_number": 2, "change_id": "Itwo", "status": "NEW", "project": "kv",
                     "branch": "master", "subject": "Child", "topic": "t"},
                    {"_number": 1, "change_id": "Ione", "status": "NEW", "project": "kv",
                     "branch": "master", "subject": "Parent"}]
        g = FakeGerrit({
            "/changes/2/revisions/current/related": {"changes": [
                self.entry(2, "c2", parent="p5"), self.entry(1, "p5", ps=5, cur=6)]},
            "/changes/": query,
        })
        out = run(g, "related", "2")
        self.assertIn("[series has PS5]", out)
        self.assertIn("needs rebase", out)
        self.assertIn("(no topic)", out)
        topic = out.split("Same topic t outside the series (1):")[1]
        self.assertIn("Other repo", topic)
        self.assertNotIn("Parent", topic)


# -------------------------------------------------------------- change lists

class ChangeLineTest(unittest.TestCase):
    def test_line(self):
        c = {"_number": 7, "status": "NEW", "work_in_progress": True, "project": "kv",
             "branch": "master", "subject": "S", "owner": ME, "updated": T4,
             "current_revision": "s", "revisions": {"s": {"_number": 2}},
             "labels": {"Verified": {"all": [{"value": 1}]},
                        "Code-Review": {"all": [{"value": 2}, {"value": -1}]}}}
        line = gq.change_line(c)
        for part in ("NEW/WIP", "PS2", "V+1 CR-1", "0 unresolved", "kv/master"):
            self.assertIn(part, line)

    def test_label_score(self):
        self.assertEqual(gq.label_score({"all": [{"value": 1}, {"value": -1}, {"value": 2}]}), -1)
        self.assertEqual(gq.label_score({"all": [{"value": 1}, {"value": 2}]}), 2)
        self.assertEqual(gq.label_score({"all": [{"value": 0}]}), 0)


# -------------------------------------------------------------------- change

class ChangeTest(unittest.TestCase):
    def test_since_ps_keeps_late_reply_to_older_patchset(self):
        out = run(FakeGerrit(change_routes()), "change", "100", "--since-ps", "4")
        self.assertIn("Done", out)                # m3: tagged PS3, posted after PS4
        self.assertNotIn("Is this needed?", out)  # m1: before PS4
        self.assertIn("1 earlier hidden", out)

    def test_json_applies_the_same_filter(self):
        import json
        data = json.loads(run(FakeGerrit(change_routes()), "--json", "change", "100",
                              "--since-ps", "4"))
        self.assertEqual([m["id"] for m in data["messages"]], ["m2", "b1", "b2", "m3"])
        self.assertEqual([cm["id"] for cm in data["comments"]["src/a.cc"]], ["c2"])
        unfiltered = json.loads(run(FakeGerrit(change_routes()), "--json", "change", "100"))
        self.assertEqual(len(unfiltered["messages"]), 5)

    def test_activity_cutoff(self):
        c = change_fixture()
        ns = lambda **kw: type("A", (), {"since_ps": None, "after": None, **kw})
        self.assertEqual(gq.activity_cutoff(c, "100", ns(since_ps=4)), T4)
        self.assertEqual(gq.activity_cutoff(c, "100", ns(since_ps=4, after="2026-12-01")),
                         "2026-12-01")
        self.assertEqual(gq.activity_cutoff(c, "100", ns()), "")
        with self.assertRaisesRegex(gq.UsageError, "no patchset 9"):
            gq.activity_cutoff(c, "100", ns(since_ps=9))

    def test_thread_state_code_and_build_started(self):
        out = run(FakeGerrit(change_routes()), "change", "100")
        # The root was posted unresolved but the thread was resolved later.
        self.assertIn("comment PS3 src/a.cc:51 [thread resolved]:", out)
        self.assertNotIn("UNRESOLVED", out)
        self.assertIn("|    51  template <typename U = T>", out)
        self.assertIn("Build Started x2", out)
        self.assertIn("ref: refs/changes/00/100/4", out)
        self.assertNotIn("Same Change-Id", out)  # empty sibling lists are left out

    def test_base_side_comment(self):
        out = run(FakeGerrit(change_routes(comments=BASE_SIDE_COMMENTS)), "change", "100")
        self.assertIn("comment PS3 src/a.cc:51 (base) [thread resolved]:", out)
        self.assertIn('|    51  "ep_removed_stat",', out)

    def test_parent_on_older_patchset_of_open_change(self):
        parent = {"_number": 99, "status": "NEW", "current_revision": "e" * 40,
                  "revisions": {"e" * 40: {"_number": 6}}}
        routes = change_routes(change_fixture(status="NEW"))
        routes["/changes/"] = lambda p: [parent] if p["q"].startswith("commit:") else []
        out = run(FakeGerrit(routes), "change", "100")
        self.assertIn("[older patchset of change 99, NEW; current is PS6: needs rebase]", out)
        parent["current_revision"] = "f" * 40  # the parent commit is now current
        out = run(FakeGerrit(routes), "change", "100")
        self.assertIn("Parent  [change 99, NEW]\n", out)

    def test_parent_note_for_open_change(self):
        routes = change_routes(change_fixture(status="NEW"))
        routes["/projects/kv_engine/branches/master"] = {"revision": "t" * 40}
        out = run(FakeGerrit(routes), "change", "100")
        self.assertIn("parent: ffffffffffff Parent  [master tip is now tttttttttttt]", out)

    def test_no_related_skips_extra_requests(self):
        import json
        routes = change_routes(change_fixture(status="NEW"))
        g = FakeGerrit(routes)
        out = run(g, "change", "100", "--no-related")
        self.assertEqual([p for p, _ in g.calls], ["/changes/100", "/changes/100/comments",
                                                   "/changes/100/robotcomments"])
        self.assertIn("parent: ffffffffffff Parent\n", out)
        self.assertIn("Is this needed?", out)
        g = FakeGerrit(routes)
        out = run(g, "change", "100", "--no-related", "--no-comments")
        self.assertEqual([p for p, _ in g.calls], ["/changes/100"])
        self.assertIn("Build Started x2", out)
        data = json.loads(run(FakeGerrit(routes), "--json", "change", "100", "--no-related"))
        self.assertNotIn("siblings", data)


# ------------------------------------------------------------------ comments

class CommentsTest(unittest.TestCase):
    def test_since_ps_marks_new_and_hides_old_threads(self):
        comments = dict(COMMENTS)
        comments["src/old.cc"] = [comment("o1", "2026-11-01 09:00:00.000000000",
                                          "Old thread", False, "m0", line=3)]
        out = run(FakeGerrit(change_routes(comments=comments)), "comments", "100",
                  "--since-ps", "4")
        self.assertIn("Done", out)
        self.assertIn("15:03 (new):", out)
        self.assertNotIn("Old thread", out)
        self.assertIn("1 shown", out)

    def test_thread_state_on_root_and_patchset_level_label(self):
        comments = dict(COMMENTS)
        comments["/PATCHSET_LEVEL"] = [comment("p1", T4, "LGTM", False, "m2")]
        out = run(FakeGerrit(change_routes(comments=comments)), "comments", "100")
        self.assertIn("PS3 L51 [thread resolved]", out)
        self.assertIn("\npatchset-level\n", out)
        self.assertIn("PS3 [thread resolved]", out)  # no "file" label for patchset-level

    def test_base_side_comment(self):
        out = run(FakeGerrit(change_routes(comments=BASE_SIDE_COMMENTS)), "comments", "100")
        self.assertIn("PS3 L51 (base) [thread resolved]", out)
        out = run(FakeGerrit(change_routes()), "comments", "100")
        self.assertNotIn("(base)", out)

    def test_falls_back_without_context_on_400(self):
        def comments(p):
            if "enable-context" in p:
                raise gq.BadStatus("HTTP 400", 400)
            return {"f": []}
        self.assertEqual(gq.get_comments(FakeGerrit({"/changes/1/comments": comments}), "1"),
                         ({"f": []}, ""))

    def test_other_errors_are_not_retried(self):
        def comments(p):
            raise gq.BadStatus("HTTP 500", 500)
        g = FakeGerrit({"/changes/1/comments": comments})
        with self.assertRaises(gq.BadStatus):
            gq.get_comments(g, "1")
        self.assertEqual(len(g.calls), 1)


ROBOT = {"_account_id": 3, "name": "Couchbase Robot"}
# Shape seen on review.couchbase.org 256108: no unresolved, robot_id and robot_run_id set.
ROBOT_COMMENTS = {
    "src/a.cc": [{"id": "r1", "robot_id": "droid-review", "robot_run_id": "run-1",
                  "author": ROBOT, "change_message_id": "m1", "patch_set": 3, "line": 51,
                  "updated": "2026-11-02 11:00:00.000000000", "message": "Lock nesting here."}],
}


def robot_routes(**kw):
    comments = {"src/a.cc": [comment("h1", T4, "Fixed, thanks <GERRIT_ACCOUNT_3>", False,
                                     "m2", reply_to="r1", line=51)]}
    routes = change_routes(comments=comments, **kw)
    routes["/changes/100/robotcomments"] = ROBOT_COMMENTS
    return routes


class RobotCommentTest(unittest.TestCase):
    def test_comments_threads_human_reply_under_robot(self):
        out = run(FakeGerrit(robot_routes()), "comments", "100")
        self.assertIn("PS3 L51 [thread resolved] Couchbase Robot [robot: droid-review]", out)
        self.assertIn("Lock nesting here.", out)
        # The reply is nested under the robot comment, and the placeholder is expanded
        # from the robot comment's author.
        self.assertIn("    ↳ [resolved] Alice", out)
        self.assertIn("Fixed, thanks Couchbase Robot", out)

    def test_change_lists_robot_comment_under_its_message(self):
        out = run(FakeGerrit(robot_routes()), "change", "100", "--no-related")
        self.assertIn("comment PS3 src/a.cc:51 [thread resolved] [robot: droid-review]:", out)
        self.assertIn("Lock nesting here.", out)

    def test_no_robot_skips_the_request(self):
        g = FakeGerrit(robot_routes())
        out = run(g, "comments", "100", "--no-robot")
        self.assertNotIn("/changes/100/robotcomments", [p for p, _ in g.calls])
        self.assertNotIn("Lock nesting", out)
        g = FakeGerrit(robot_routes())
        run(g, "change", "100", "--no-related", "--no-robot")
        self.assertNotIn("/changes/100/robotcomments", [p for p, _ in g.calls])

    def test_missing_endpoint_is_tolerated(self):
        out = run(FakeGerrit(change_routes()), "comments", "100")
        self.assertIn("Is this needed?", out)
        self.assertNotIn("unavailable", out)  # 404: nothing to report

    def test_refused_endpoint_is_tolerated(self):
        def refuse(p):
            raise gq.BadStatus("HTTP 405", 405)
        routes = change_routes()
        routes["/changes/100/robotcomments"] = refuse
        out = run(FakeGerrit(routes), "comments", "100")
        self.assertIn("Is this needed?", out)
        self.assertIn("2 comments in 1 files (robot comments unavailable: HTTP 405)\n", out)
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            json.loads(run(FakeGerrit(routes), "comments", "100", "--json"))
        self.assertEqual(err.getvalue(), "note: robot comments unavailable: HTTP 405\n")
        out = run(FakeGerrit(routes), "change", "100", "--no-related")
        self.assertIn("Is this needed?", out)
        self.assertIn("\nMessages: (robot comments unavailable: HTTP 405)\n", out)
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            json.loads(run(FakeGerrit(routes), "change", "100", "--no-related", "--json"))
        self.assertEqual(err.getvalue(), "note: robot comments unavailable: HTTP 405\n")

    def test_routes_are_not_mutated(self):
        routes = robot_routes()
        before = len(routes["/changes/100/comments"]["src/a.cc"])
        run(FakeGerrit(routes), "comments", "100")
        self.assertEqual(len(routes["/changes/100/comments"]["src/a.cc"]), before)


class CommentsFileTest(unittest.TestCase):
    def routes(self):
        comments = dict(COMMENTS)
        comments["/PATCHSET_LEVEL"] = [comment("p1", T4, "LGTM", False, "m2")]
        comments["src/b.cc"] = [comment("b1", T4, "Other file", True, "m2", line=3)]
        return change_routes(comments=comments)

    def test_one_file(self):
        out = run(FakeGerrit(self.routes()), "comments", "100", "--file", "src/a.cc")
        self.assertIn("Is this needed?", out)
        self.assertNotIn("Other file", out)
        self.assertNotIn("LGTM", out)
        self.assertIn("0 unresolved thread(s) in src/a.cc", out)

    def test_patchset_level(self):
        out = run(FakeGerrit(self.routes()), "comments", "100", "--file", "/PATCHSET_LEVEL")
        self.assertIn("LGTM", out)
        self.assertNotIn("Is this needed?", out)

    def test_unknown_file_lists_the_others(self):
        with self.assertRaisesRegex(gq.UsageError, r"no comments on x\.cc; files with comments: "
                                                   r"/PATCHSET_LEVEL, src/a\.cc, src/b\.cc"):
            run(FakeGerrit(self.routes()), "comments", "100", "--file", "x.cc")


BOB = {"_account_id": 4, "name": "Bob Builder", "username": "bob", "email": "bob@example.org"}


class CommentsAuthorTest(unittest.TestCase):
    def routes(self):
        reply = comment("c3", "2026-11-02 16:00:00.000000000", "Agreed", False, "m2",
                        reply_to="c1", line=51)
        reply["author"] = BOB
        bob = comment("b1", T4, "Bob's thread", True, "m2", line=3)
        bob["author"] = BOB
        return change_routes(comments={"src/a.cc": [*COMMENTS["src/a.cc"], reply],
                                       "src/b.cc": [bob]})

    def test_whole_threads_with_a_matching_comment(self):
        out = run(FakeGerrit(self.routes()), "comments", "100", "--author", "BOB@example")
        self.assertIn("Is this needed?", out)  # Alice's root, kept for Bob's reply
        self.assertIn("Bob's thread", out)
        self.assertIn("Showing threads with a comment by 'BOB@example'.", out)
        out = run(FakeGerrit(self.routes()), "comments", "100", "--author", "alice")
        self.assertIn("Agreed", out)  # Bob's reply stays with Alice's thread
        self.assertNotIn("Bob's thread", out)
        self.assertIn("; 1 shown", out)

    def test_robot_id(self):
        out = run(FakeGerrit(robot_routes()), "comments", "100", "--author", "droid-review")
        self.assertIn("Lock nesting here.", out)

    def test_author_and_cutoff_must_hold_for_the_same_comment(self):
        # The src/a.cc thread has activity after 15:30 (Bob's reply) and comments by
        # Alice, but none by Alice after 15:30, so it is hidden.
        out = run(FakeGerrit(self.routes()), "comments", "100", "--author", "alice",
                  "--after", "2026-11-02 15:30")
        self.assertIn("(no matching threads)", out)
        self.assertIn("; 0 shown", out)
        out = run(FakeGerrit(self.routes()), "comments", "100", "--author", "bob",
                  "--after", "2026-11-02 15:30")
        self.assertIn("Is this needed?", out)  # kept whole for Bob's 16:00 reply
        self.assertNotIn("Bob's thread", out)  # Bob's, but posted at 15:03

    def test_author_with_since_ps(self):
        # PS4 was uploaded at 15:03:38. Alice's c1 is older; her c2 (15:03:45) isn't.
        routes = self.routes()
        lst = routes["/changes/100/comments"]["src/a.cc"]
        lst[1] = {**lst[1], "updated": T3}  # a copy: the fixture is shared
        out = run(FakeGerrit(routes), "comments", "100", "--author", "alice", "--since-ps", "4")
        self.assertIn("(no matching threads)", out)  # Bob's later reply doesn't count
        out = run(FakeGerrit(self.routes()), "comments", "100", "--author", "alice",
                  "--since-ps", "4")
        self.assertIn("Is this needed?", out)

    def test_json_matches_text(self):
        import json
        out = run(FakeGerrit(self.routes()), "comments", "100", "--author", "alice", "--json")
        data = json.loads(out)
        self.assertEqual(list(data), ["src/a.cc"])
        self.assertEqual([cm["id"] for cm in data["src/a.cc"]], ["c1", "c2", "c3"])

    def test_validator(self):
        for bad in ("", "a\nb", "x" * 101):
            with self.assertRaises(Exception, msg=bad):
                gq.author_arg(bad)


class AccountPlaceholderTest(unittest.TestCase):
    def test_lookup_once_and_fallback(self):
        g = FakeGerrit({"/accounts/7": {"_account_id": 7, "name": "Bob"}})
        names = {"1": "Alice"}
        text = "<GERRIT_ACCOUNT_1> <GERRIT_ACCOUNT_7> <GERRIT_ACCOUNT_7> <GERRIT_ACCOUNT_9>"
        self.assertEqual(gq.expand_accounts(g, text, names),
                         "Alice Bob Bob <GERRIT_ACCOUNT_9>")
        self.assertEqual([p for p, _ in g.calls], ["/accounts/7", "/accounts/9"])

    def test_names_fall_back_to_username_and_email(self):
        names = gq.account_names([{"_account_id": 5, "username": "carol"},
                                  {"_account_id": 6, "email": "dan@example.org"},
                                  {"_account_id": 8}])
        self.assertEqual(names, {"5": "carol", "6": "dan@example.org"})
        # A full name wins over a username seen earlier for the same account.
        names = gq.account_names([{"_account_id": 5, "username": "carol"},
                                  {"_account_id": 5, "name": "Carol C", "username": "carol"}])
        self.assertEqual(names, {"5": "Carol C"})


class QueryTest(unittest.TestCase):
    def test_start_is_sent_and_named_for_the_next_page(self):
        hits = [{**change_fixture(), "_number": n} for n in (1, 2)]
        hits[-1]["_more_changes"] = True
        g = FakeGerrit({"/changes/": lambda p: hits})
        out = run(g, "query", "project:kv_engine", "-n", "2", "--start", "4")
        self.assertIn(("S", 4), g.calls[0][1])
        self.assertIn("(more results available; pass --start 6)", out)
        g = FakeGerrit({"/changes/": lambda p: hits})
        run(g, "query", "project:kv_engine")
        self.assertNotIn("S", dict(g.calls[0][1]))


# ---------------------------------------------------------------------- show

class ShowTest(unittest.TestCase):
    PATH = "/changes/100/revisions/current/files/f.h/content"
    TEXT = "\n".join(f"l{i}" for i in range(1, 11)) + "\n"

    def test_header_names_printed_lines(self):
        out = run(FakeGerrit({self.PATH: self.TEXT}), "show", "100", "f.h", "--max-lines", "3")
        self.assertIn("(lines 1-3 of 10)", out)
        self.assertIn("    3  l3", out)
        self.assertNotIn("l4", out)
        self.assertIn("truncated: 7 more lines", out)

    def test_range_clamped_to_file(self):
        out = run(FakeGerrit({self.PATH: self.TEXT}), "show", "100", "f.h", "--lines", "9-20")
        self.assertIn("(lines 9-10 of 10)", out)
        self.assertNotIn("truncated", out)

    def test_numbers_lines_like_gerrit(self):
        # \f and \u2028 are not line breaks to Gerrit; splitlines() would treat them as such.
        out = run(FakeGerrit({self.PATH: "a\fb\nc\u2028d\ne\n"}), "show", "100", "f.h")
        self.assertIn("(lines 1-3 of 3)", out)
        self.assertIn("    3  e", out)

    def test_no_trailing_newline(self):
        out = run(FakeGerrit({self.PATH: "a\nb"}), "show", "100", "f.h")
        self.assertIn("(lines 1-2 of 2)", out)

    def test_empty_file(self):
        out = run(FakeGerrit({self.PATH: ""}), "show", "100", "f.h")
        self.assertEqual(out, "Change 100 current patchset: f.h (empty file)\n")

    def test_empty_file_json(self):
        import json
        data = json.loads(run(FakeGerrit({self.PATH: ""}), "--json", "show", "100", "f.h"))
        self.assertEqual((data["lines"], data["total_lines"]), ([], 0))

    def test_crlf(self):
        g = FakeGerrit({self.PATH: "a\r\nb\r\n"})
        self.assertEqual(run(g, "show", "100", "f.h").splitlines()[1:], ["    1  a", "    2  b"])
        import json
        self.assertEqual(json.loads(run(g, "--json", "show", "100", "f.h"))["lines"], ["a", "b"])

    def test_header_names_patchset(self):
        path = "/changes/100/revisions/2/files/f.h/content"
        out = run(FakeGerrit({path: self.TEXT}), "show", "100", "f.h", "--ps", "2")
        self.assertTrue(out.startswith("Change 100 patchset 2: f.h"))

    def test_range_past_end(self):
        with self.assertRaisesRegex(gq.UsageError, "has only 10 lines"):
            run(FakeGerrit({self.PATH: self.TEXT}), "show", "100", "f.h", "--lines", "11-12")

    def show(self, *argv, text=TEXT):
        return run(FakeGerrit({self.PATH: text}), "show", "100", "f.h", *argv)

    def test_several_ranges(self):
        out = self.show("--lines", "7-8", "--lines", "2-3")
        self.assertEqual(out.splitlines(), [
            "Change 100 current patchset: f.h (lines 2-3, 7-8 of 10)",
            "    2  l2", "    3  l3", "--", "    7  l7", "    8  l8"])

    def test_overlapping_ranges_merge(self):
        self.assertIn("(lines 2-6 of 10)", self.show("--lines", "2-4", "--lines", "3-6"))
        self.assertIn("(lines 2-5 of 10)", self.show("--lines", "2-3", "--lines", "4-5"))

    def test_any_range_past_end(self):
        with self.assertRaisesRegex(gq.UsageError, "has only 10 lines"):
            self.show("--lines", "1-2", "--lines", "11-12")

    def test_several_ranges_json(self):
        import json
        data = json.loads(self.show("--json", "--lines", "2-3", "--lines", "9-20"))
        self.assertEqual(data["groups"], [{"first_line": 2, "lines": ["l2", "l3"]},
                                          {"first_line": 9, "lines": ["l9", "l10"]}])

    def test_no_line_numbers(self):
        self.assertEqual(self.show("--no-line-numbers", "--lines", "1-2").splitlines()[1:],
                         ["l1", "l2"])

    def test_parent(self):
        g = FakeGerrit({self.PATH: lambda p: "old\n" if p.get("parent") == 1 else "new\n"})
        out = run(g, "show", "100", "f.h", "--parent")
        self.assertTrue(out.startswith("Change 100 parent of current patchset: f.h"))
        self.assertIn("    1  old", out)
        self.assertEqual(g.calls, [(self.PATH, [("parent", 1)])])

    def test_grep(self):
        out = self.show("--grep", "l[37]$")
        self.assertEqual(out.splitlines(), [
            "Change 100 current patchset: f.h (2 matching lines of 10)",
            "    3: l3", "--", "    7: l7"])

    def test_grep_context_merges_and_separates(self):
        out = self.show("--grep", "^l(2|4|9)$", "--context", "1")
        self.assertEqual(out.splitlines()[1:], [
            "    1  l1", "    2: l2", "    3  l3", "    4: l4", "    5  l5", "--",
            "    8  l8", "    9: l9", "   10  l10"])

    def test_grep_within_ranges(self):
        out = self.show("--grep", "l", "--context", "2", "--lines", "4-5")
        self.assertEqual(out.splitlines(), [
            "Change 100 current patchset: f.h (2 matching lines in lines 4-5 of 10)",
            "    4: l4", "    5: l5"])

    def test_grep_no_matches(self):
        self.assertEqual(self.show("--grep", "zzz").splitlines()[1:], ["(no matches)"])

    def test_grep_json(self):
        import json
        data = json.loads(self.show("--json", "--grep", "l3$", "--context", "1"))
        self.assertEqual(data["matches"], [3])
        self.assertEqual(data["groups"], [{"first_line": 2, "lines": ["l2", "l3", "l4"]}])

    def test_grep_bad_regex(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            self.show("--grep", "(")

    def test_context_needs_grep(self):
        with self.assertRaisesRegex(gq.UsageError, "--context needs --grep"):
            self.show("--context", "2")


# ----------------------------------------------------------------- HTTP layer

class FakeOpener:
    """Answers paths in `missing` with 404, raises `fail` for paths in `failing`."""
    def __init__(self, missing=(), failing=(), fail=None, body=b")]}'\n{}"):
        self.missing, self.failing, self.fail, self.body = set(missing), set(failing), fail, body

    def open(self, req, timeout=None):
        path = req.full_url.split("review.example.org", 1)[1].split("?")[0]
        if path in self.failing:
            raise self.fail
        if path in self.missing:
            raise urllib.error.HTTPError(req.full_url, 404, "Not Found", {}, io.BytesIO())
        return io.BytesIO(self.body)


def gerrit_with(opener):
    g = gq.Gerrit("review.example.org")
    g.opener = opener
    return g


class NotFoundTest(unittest.TestCase):
    CONTENT = "/changes/1/revisions/9/files/x.h/content"

    def error(self, opener, path=CONTENT, params=None):
        with self.assertRaises(gq.NotFound) as ctx:
            gerrit_with(opener).get(path, params)
        return str(ctx.exception)

    def test_missing_change(self):
        self.assertIn("change 1 not found", self.error(FakeOpener(
            missing={self.CONTENT, "/changes/1"})))

    def test_missing_patchset(self):
        self.assertEqual(self.error(FakeOpener(
            missing={self.CONTENT, "/changes/1/revisions/9/files"})),
            "change 1 has no patchset 9")

    def test_missing_base_patchset(self):
        path = "/changes/1/revisions/current/files"
        self.assertEqual(self.error(FakeOpener(
            missing={path, "/changes/1/revisions/7/files"}), path, [("base", 7)]),
            "change 1 has no patchset 7 (--base)")

    def test_missing_file(self):
        self.assertEqual(self.error(FakeOpener(missing={self.CONTENT})),
                         "file x.h not found in patchset 9 of change 1")

    def test_missing_file_in_parent(self):
        self.assertEqual(self.error(FakeOpener(missing={self.CONTENT}), params=[("parent", 1)]),
                         "file x.h not found in the parent of patchset 9 of change 1 "
                         "(added by this change?)")

    def test_missing_file_in_current_names_its_patchset(self):
        path = "/changes/1/revisions/current/files/x.h/content"
        body = b')]}\'\n{"current_revision": "abc", "revisions": {"abc": {"_number": 3}}}'
        self.assertEqual(self.error(FakeOpener(missing={path}, body=body), path),
                         "file x.h not found in patchset 3 of change 1")

    def test_failed_probe_falls_back_to_plain_message(self):
        for fail in (TimeoutError("t"), ConnectionResetError(), RuntimeError("boom"),
                     urllib.error.HTTPError("u", 500, "err", {}, io.BytesIO())):
            msg = self.error(FakeOpener(missing={self.CONTENT}, failing={"/changes/1"},
                                        fail=fail))
            self.assertEqual(msg, f"not found: {self.CONTENT}", type(fail).__name__)


class RequestErrorTest(unittest.TestCase):
    def test_unwrapped_network_errors_are_usage_errors(self):
        for fail in (TimeoutError("read timed out"), ConnectionResetError()):
            g = gerrit_with(FakeOpener(failing={"/changes/1"}, fail=fail))
            with self.assertRaisesRegex(gq.UsageError, "request failed"):
                g.get("/changes/1")

    def test_non_json(self):
        with self.assertRaisesRegex(gq.UsageError, "not JSON"):
            gerrit_with(FakeOpener(body=b"<html>")).get("/changes/1")

    def test_other_status_is_bad_status(self):
        fail = urllib.error.HTTPError("u", 503, "x", {}, io.BytesIO())
        with self.assertRaises(gq.BadStatus) as ctx:
            gerrit_with(FakeOpener(failing={"/changes/1"}, fail=fail)).get("/changes/1")
        self.assertEqual(ctx.exception.code, 503)


if __name__ == "__main__":
    unittest.main()
