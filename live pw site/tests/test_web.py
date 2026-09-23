"""
Tests for the request layer: chat, notes, PIN handling and a route smoke test.

Everything that writes runs against a temporary directory. No test in this file
may touch the real chat log, notes, prefs or league history.

The route smoke test deliberately covers only pages that need no network. The
market pages (/odds, /trends, /analyzer, /player/...) reach out to Sleeper,
FantasyCalc and KeepTradeCut, which makes them slow and dependent on someone
else's uptime — they are exercised by deploy.py's post-deploy checks instead.
"""

import json
import os
import re
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pwoods_site as P


class WebTestCase(unittest.TestCase):
    """Points every mutable file at a temp dir and hands back a test client."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="pwtest-")
        self._saved = {name: getattr(P, name) for name in
                       ("CHAT_LOG_FILE", "CHAT_USERS_FILE", "NOTES_FILE", "PREFS_FILE")}
        for name, filename in (("CHAT_LOG_FILE", "chat_log.json"),
                               ("CHAT_USERS_FILE", "chat_users.json"),
                               ("NOTES_FILE", "league_notes.json"),
                               ("PREFS_FILE", "user_prefs.json")):
            setattr(P, name, os.path.join(self.tmp, filename))
        P._pin_failures.clear()
        P.app.config["TESTING"] = True
        self.client = P.app.test_client()

    def tearDown(self):
        for name, value in self._saved.items():
            setattr(P, name, value)
        P._pin_failures.clear()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def post_message(self, text, username="ann", pin="1234"):
        return self.client.post('/chat', json={"username": username,
                                               "pin": pin, "message": text})

    def read_log(self):
        with open(P.CHAT_LOG_FILE, encoding="utf-8") as f:
            return json.load(f)


class TestChatCursor(WebTestCase):
    """The client tracks an absolute position in the log, because the server
    only returns a trailing window. Counting what arrived instead means the
    count stops changing once the log passes that window — which froze the chat
    permanently at 200 messages."""

    def test_first_index_reports_the_start_of_the_window(self):
        for i in range(P.CHAT_WINDOW + 60):
            self.post_message(f"m{i}")
        payload = self.client.get('/get_messages').get_json()
        self.assertEqual(len(payload["messages"]), P.CHAT_WINDOW)
        self.assertEqual(payload["first_index"], 60)

    def test_new_messages_are_visible_past_the_window(self):
        for i in range(P.CHAT_WINDOW + 60):
            self.post_message(f"m{i}")
        payload = self.client.get('/get_messages').get_json()
        cursor = payload["first_index"] + len(payload["messages"])

        self.post_message("brand new")
        payload = self.client.get('/get_messages').get_json()
        unseen = [m["message"] for m in payload["messages"][cursor - payload["first_index"]:]]
        self.assertEqual(unseen, ["brand new"])

    def test_empty_log_is_a_valid_response(self):
        payload = self.client.get('/get_messages').get_json()
        self.assertEqual(payload, {"messages": [], "first_index": 0})


class TestChatLimits(WebTestCase):

    def test_message_length_is_capped(self):
        resp = self.post_message("x" * (P.CHAT_MAX_MESSAGE_CHARS + 1))
        self.assertEqual(resp.status_code, 400)

    def test_message_at_the_limit_is_accepted(self):
        self.assertEqual(self.post_message("x" * P.CHAT_MAX_MESSAGE_CHARS).status_code, 200)

    def test_empty_message_is_rejected(self):
        self.assertEqual(self.post_message("   ").status_code, 400)

    def test_log_is_trimmed_so_it_cannot_grow_forever(self):
        """The whole file is rewritten under a lock on every post, so an
        unbounded log slows down every send."""
        original = P.CHAT_KEEP_MESSAGES
        P.CHAT_KEEP_MESSAGES = 10
        try:
            for i in range(25):
                self.post_message(f"m{i}")
            log = self.read_log()
            self.assertEqual(len(log), 10)
            self.assertEqual(log[-1]["message"], "m24")   # newest kept
            self.assertEqual(log[0]["message"], "m15")    # oldest dropped
        finally:
            P.CHAT_KEEP_MESSAGES = original


class TestPinHandling(WebTestCase):

    def test_first_use_registers_the_username(self):
        self.assertEqual(self.post_message("hello").status_code, 200)

    def test_wrong_pin_is_rejected(self):
        self.post_message("hello")
        self.assertEqual(self.post_message("hi", pin="9999").status_code, 403)

    def test_repeated_failures_trigger_a_lockout(self):
        self.post_message("hello")
        codes = [self.post_message("x", pin="9999").status_code for _ in range(7)]
        self.assertEqual(codes[:P.PIN_MAX_FAILURES], [403] * P.PIN_MAX_FAILURES)
        self.assertTrue(all(c == 429 for c in codes[P.PIN_MAX_FAILURES:]))

    def test_lockout_blocks_even_the_correct_pin(self):
        """Otherwise an attacker just keeps guessing until one lands."""
        self.post_message("hello")
        for _ in range(P.PIN_MAX_FAILURES):
            self.post_message("x", pin="9999")
        self.assertEqual(self.post_message("legit").status_code, 429)

    def test_success_clears_the_failure_count(self):
        self.post_message("hello")
        for _ in range(P.PIN_MAX_FAILURES - 1):
            self.post_message("x", pin="9999")
        self.assertEqual(self.post_message("legit").status_code, 200)
        self.assertNotIn("ann", P._pin_failures)

    def test_lockout_is_per_username(self):
        self.post_message("hello", username="ann")
        for _ in range(P.PIN_MAX_FAILURES + 1):
            self.post_message("x", username="ann", pin="9999")
        self.assertEqual(self.post_message("hi", username="bob").status_code, 200)


class TestNotes(WebTestCase):

    def add_note(self, text="a note", username="ann", pin="1234", year="2025"):
        return self.client.post('/api/notes', json={
            "username": username, "pin": pin, "year": year, "text": text})

    def note_ids(self):
        with open(P.NOTES_FILE, encoding="utf-8") as f:
            return [n["id"] for notes in json.load(f).values() for n in notes]

    def test_add_and_delete_own_note(self):
        self.assertEqual(self.add_note().status_code, 200)
        note_id = self.note_ids()[0]
        resp = self.client.post('/api/notes/delete',
                                json={"username": "ann", "pin": "1234", "id": note_id})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(self.note_ids(), [])

    def test_cannot_delete_someone_elses_note(self):
        self.add_note(username="ann")
        note_id = self.note_ids()[0]
        resp = self.client.post('/api/notes/delete',
                                json={"username": "bob", "pin": "5678", "id": note_id})
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(len(self.note_ids()), 1)

    def test_deleting_a_missing_note_is_a_404(self):
        self.add_note()
        resp = self.client.post('/api/notes/delete',
                                json={"username": "ann", "pin": "1234", "id": "nope"})
        self.assertEqual(resp.status_code, 404)

    def test_deleting_the_last_note_of_a_year_removes_the_year(self):
        """Exercises the branch that deletes a dict key — it used to do that
        while iterating the same dict."""
        self.add_note(year="2025")
        self.client.post('/api/notes/delete',
                         json={"username": "ann", "pin": "1234", "id": self.note_ids()[0]})
        with open(P.NOTES_FILE, encoding="utf-8") as f:
            self.assertEqual(json.load(f), {})

    def test_invalid_year_is_rejected(self):
        self.assertEqual(self.add_note(year="not-a-year").status_code, 400)
        self.assertEqual(self.add_note(year="1500").status_code, 400)

    def test_empty_and_oversized_notes_are_rejected(self):
        self.assertEqual(self.add_note(text="  ").status_code, 400)
        self.assertEqual(self.add_note(text="x" * 20001).status_code, 400)


class TestAtomicWrites(WebTestCase):
    """A plain open(path, "w") truncates before writing, so a crash mid-write
    destroys the file. Everything goes through a temp file and a rename."""

    def test_no_temp_file_is_left_behind(self):
        self.post_message("hello")
        self.assertFalse(os.path.exists(P.CHAT_LOG_FILE + ".tmp"))

    def test_content_round_trips_including_non_ascii(self):
        self.post_message("emoji 🏈 and accents é")
        self.assertEqual(self.read_log()[-1]["message"], "emoji 🏈 and accents é")


class TestRoutesSmoke(WebTestCase):
    """Every page that renders from league_history.json alone must return 200
    against the real data. This is the check that would have caught the
    playoff-stats crash before it reached the site."""

    OFFLINE_ROUTES = ["/", "/records", "/head_to_head", "/power_rankings",
                      "/draft", "/rosters", "/notes", "/chat", "/api/prefs"]

    def test_offline_pages_render(self):
        for route in self.OFFLINE_ROUTES:
            with self.subTest(route=route):
                self.assertEqual(self.client.get(route).status_code, 200)

    def test_every_season_page_renders(self):
        for year in sorted(P.league_data):
            with self.subTest(year=year):
                self.assertEqual(self.client.get(f"/rosters/{year}").status_code, 200)

    def test_every_owner_page_renders(self):
        owners = {t["owner"] for y in P.league_data.values() for t in y.get("teams", [])}
        for owner in sorted(owners):
            with self.subTest(owner=owner):
                self.assertEqual(self.client.get(f"/owner/{owner}").status_code, 200)

    def test_unknown_resources_are_404_not_500(self):
        for route in ("/owner/Nobody", "/rosters/1999", "/player/000000"):
            with self.subTest(route=route):
                self.assertEqual(self.client.get(route).status_code, 404)


class TestOwnerIdentity(WebTestCase):
    """The "I'm…" preference: who the visitor claims to be, and its blast radius.

    The stored name is compared against owner names all over the templates, so
    anything that isn't a real league member has to be rejected at the door —
    otherwise a typo silently highlights nothing for good, and arbitrary text
    reaches the page through the settings menu.
    """

    ME = "Roldan Navarrete"

    def _rows_marked(self, route):
        html = self.client.get(route).get_data(as_text=True)
        return len(re.findall(r'<tr[^>]*class="[^"]*me-row', html))

    def test_a_real_owner_is_stored(self):
        self.client.get("/")                       # issues the pw-uid cookie
        self.client.post("/api/prefs", json={"owner": self.ME})
        self.assertEqual(self.client.get("/api/prefs").get_json()["owner"], self.ME)

    def test_an_unknown_name_is_rejected(self):
        self.client.get("/")
        self.client.post("/api/prefs", json={"owner": "Totally Fake Guy"})
        self.assertEqual(self.client.get("/api/prefs").get_json()["owner"], "")

    def test_markup_cannot_be_smuggled_in(self):
        self.client.get("/")
        self.client.post("/api/prefs", json={"owner": "<script>alert(1)</script>"})
        self.assertEqual(self.client.get("/api/prefs").get_json()["owner"], "")

    def test_no_rows_are_marked_before_anyone_is_picked(self):
        self.client.get("/")
        for route in ("/records", "/power_rankings", "/year/2025"):
            with self.subTest(route=route):
                self.assertEqual(self._rows_marked(route), 0)

    def test_picking_an_owner_marks_their_rows(self):
        self.client.get("/")
        self.client.post("/api/prefs", json={"owner": self.ME})
        for route in ("/records", "/power_rankings", "/year/2025"):
            with self.subTest(route=route):
                self.assertGreater(self._rows_marked(route), 0)

    def test_clearing_removes_every_mark(self):
        self.client.get("/")
        self.client.post("/api/prefs", json={"owner": self.ME})
        self.client.post("/api/prefs", json={"owner": ""})
        self.assertEqual(self._rows_marked("/records"), 0)

    def test_my_team_shortcut_appears_only_once_claimed(self):
        self.client.get("/")
        self.assertNotIn("My Team", self.client.get("/").get_data(as_text=True))
        self.client.post("/api/prefs", json={"owner": self.ME})
        self.assertIn("My Team", self.client.get("/").get_data(as_text=True))

    def test_an_owner_who_leaves_the_league_stops_matching(self):
        """A stored name is re-checked on render, not trusted from the file."""
        self.client.get("/")
        self.client.post("/api/prefs", json={"owner": self.ME})
        saved = P.league_data
        try:
            P.league_data = {y: d for y, d in saved.items() if y == "2014"}
            if self.ME not in {t["owner"] for t in P.league_data["2014"]["teams"]}:
                self.assertNotIn("My Team", self.client.get("/").get_data(as_text=True))
        finally:
            P.league_data = saved


if __name__ == "__main__":
    unittest.main()
