"""
Tests for new-note alerts (push.py).

The delivery test runs a real encrypted push against a tiny local server
standing in for Apple/Google's push service, then decrypts what arrived the
way a phone would. It needs pywebpush, so it's skipped where that isn't
installed; the validation tests always run.
"""

import base64
import json
import os
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import push


def _sub(endpoint="https://push.example.com/abc"):
    return {"endpoint": endpoint, "keys": {"p256dh": "x" * 87, "auth": "y" * 22}}


class PushFiles(unittest.TestCase):
    """Point the key and subscriber files at a temp dir."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="pwpush-")
        self._saved = (push.VAPID_FILE, push.SUBS_FILE, push._vapid)
        push.VAPID_FILE = os.path.join(self.tmp, "vapid.pem")
        push.SUBS_FILE = os.path.join(self.tmp, "subs.json")
        push._vapid = None

    def tearDown(self):
        push.VAPID_FILE, push.SUBS_FILE, push._vapid = self._saved


class TestSubscriptions(PushFiles):

    def test_rejects_anything_but_a_browser_subscription(self):
        for bad in (None, "x", {}, {"endpoint": "http://insecure/x", "keys": _sub()["keys"]},
                    {"endpoint": "https://ok/x", "keys": {}},
                    {"endpoint": "https://ok/" + "a" * 2000, "keys": _sub()["keys"]}):
            with self.subTest(bad=str(bad)[:40]):
                self.assertFalse(push.subscribe(bad))

    def test_subscribing_twice_keeps_one_copy(self):
        self.assertTrue(push.subscribe(_sub()))
        self.assertTrue(push.subscribe(_sub()))
        self.assertEqual(len(push._load()), 1)

    def test_unsubscribe_removes_it(self):
        push.subscribe(_sub())
        push.unsubscribe(_sub()["endpoint"])
        self.assertEqual(push._load(), [])

    def test_extra_fields_are_not_stored(self):
        s = _sub()
        s["junk"] = "<script>"
        push.subscribe(s)
        self.assertNotIn("junk", push._load()[0])


@unittest.skipUnless(push.AVAILABLE, "pywebpush not installed here")
class TestDelivery(PushFiles):

    def test_a_push_arrives_and_decrypts(self):
        import http_ece
        from cryptography.hazmat.primitives.asymmetric import ec
        from cryptography.hazmat.primitives import serialization

        # The "phone": its own key pair and auth secret, as a browser makes.
        device = ec.generate_private_key(ec.SECP256R1())
        pub = device.public_key().public_bytes(serialization.Encoding.X962,
                                               serialization.PublicFormat.UncompressedPoint)
        auth = os.urandom(16)
        b64 = lambda b: base64.urlsafe_b64encode(b).rstrip(b"=").decode()

        received = {}

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                received["headers"] = {k.lower(): v for k, v in self.headers.items()}
                received["body"] = self.rfile.read(int(self.headers["Content-Length"]))
                self.send_response(201)
                self.end_headers()

            def log_message(self, *a):
                pass

        server = HTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.handle_request, daemon=True).start()
        sub = {"endpoint": f"http://127.0.0.1:{server.server_port}/push",
               "keys": {"p256dh": b64(pub), "auth": b64(auth)}}

        push._deliver([sub], {"title": "New note", "body": "hi", "url": "/notes"})
        server.server_close()

        self.assertTrue(received["headers"].get("authorization", "").startswith("vapid "))
        self.assertEqual(received["headers"].get("content-encoding"), "aes128gcm")
        plain = http_ece.decrypt(received["body"], private_key=device,
                                 auth_secret=auth, version="aes128gcm")
        self.assertEqual(json.loads(plain), {"title": "New note", "body": "hi", "url": "/notes"})

    def test_public_key_is_a_p256_point(self):
        key = base64.urlsafe_b64decode(push.public_key() + "==")
        self.assertEqual(len(key), 65)
        self.assertEqual(key[0], 4)


if __name__ == "__main__":
    unittest.main()
