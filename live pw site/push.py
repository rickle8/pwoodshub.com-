"""
push.py — phone/desktop notifications ("web push") for the home-screen app.

Anyone can tap "Turn on alerts" and their browser hands us a subscription: a
private URL at Apple/Google/Mozilla that delivers a message to that one device.
We keep those in push_subscriptions.json and post to them when something worth
an alert happens (a new league note).

The VAPID key pair identifies this site to those push services. It's made on
the server the first time it's needed and never leaves it — like
local_config.py, neither file is in the repo or uploaded by deploy.py.

If pywebpush isn't installed, alerts are simply switched off; nothing else on
the site depends on this module.
"""

import base64
import json
import os
import threading

try:
    from pywebpush import webpush, WebPushException
    from py_vapid import Vapid
    from cryptography.hazmat.primitives import serialization
    AVAILABLE = True
except ImportError:          # alerts off, site unaffected
    AVAILABLE = False

_DIR = os.path.dirname(os.path.abspath(__file__))
VAPID_FILE = os.path.join(_DIR, "vapid_private.pem")
SUBS_FILE = os.path.join(_DIR, "push_subscriptions.json")
# Push services want a contact for the sender; the site itself will do.
CLAIMS = {"sub": "https://www.pwoodshub.com"}
MAX_SUBSCRIPTIONS = 300

_lock = threading.Lock()
_vapid = None


def _key():
    global _vapid
    if _vapid is None:
        with _lock:
            if _vapid is None:
                if not os.path.exists(VAPID_FILE):
                    v = Vapid()
                    v.generate_keys()
                    v.save_key(VAPID_FILE)
                _vapid = Vapid.from_file(VAPID_FILE)
    return _vapid


def public_key():
    """The key browsers need to subscribe, as base64url (no padding)."""
    raw = _key().public_key.public_bytes(serialization.Encoding.X962,
                                         serialization.PublicFormat.UncompressedPoint)
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _load():
    try:
        with open(SUBS_FILE) as f:
            return json.load(f)
    except (OSError, ValueError):
        return []


def _save(subs):
    tmp = SUBS_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(subs, f)
    os.replace(tmp, SUBS_FILE)


def valid_subscription(sub):
    """Only accept the shape browsers actually send, pointed at https."""
    if not isinstance(sub, dict):
        return False
    endpoint = sub.get("endpoint")
    keys = sub.get("keys") or {}
    return (isinstance(endpoint, str) and endpoint.startswith("https://")
            and len(endpoint) < 1000
            and isinstance(keys.get("p256dh"), str) and isinstance(keys.get("auth"), str)
            and len(keys["p256dh"]) < 200 and len(keys["auth"]) < 100)


def subscribe(sub):
    """Store a subscription. Returns False if it was rejected."""
    if not valid_subscription(sub):
        return False
    clean = {"endpoint": sub["endpoint"],
             "keys": {"p256dh": sub["keys"]["p256dh"], "auth": sub["keys"]["auth"]}}
    with _lock:
        subs = [s for s in _load() if s["endpoint"] != clean["endpoint"]]
        if len(subs) >= MAX_SUBSCRIPTIONS:
            return False
        subs.append(clean)
        _save(subs)
    return True


def unsubscribe(endpoint):
    with _lock:
        subs = _load()
        kept = [s for s in subs if s["endpoint"] != endpoint]
        if len(kept) != len(subs):
            _save(kept)


def _deliver(subs, payload):
    """Send to each subscription; forget the ones the push service says are gone."""
    data = json.dumps(payload)
    dead = []
    for s in subs:
        try:
            webpush(subscription_info=s, data=data, vapid_private_key=_key(),
                    vapid_claims=dict(CLAIMS), ttl=24 * 3600, timeout=10)
        except WebPushException as e:
            status = getattr(e.response, "status_code", None)
            if status in (404, 410):          # unsubscribed or expired
                dead.append(s["endpoint"])
            else:
                print(f"Push failed ({status}): {str(e)[:120]}")
        except Exception as e:
            print(f"Push failed: {type(e).__name__}: {str(e)[:120]}")
    for endpoint in dead:
        unsubscribe(endpoint)


def notify_all(title, body, url="/"):
    """Alert every subscribed device, in the background so the request that
    triggered it isn't kept waiting on a dozen push services."""
    if not AVAILABLE:
        return
    _key()
    with _lock:
        subs = _load()
    if subs:
        payload = {"title": title, "body": body, "url": url}
        threading.Thread(target=_deliver, args=(subs, payload), daemon=True).start()


def notify_one(sub, title, body, url="/"):
    """A single alert — the "you're all set" confirmation after subscribing."""
    if AVAILABLE and valid_subscription(sub):
        _key()
        threading.Thread(target=_deliver,
                         args=([sub], {"title": title, "body": body, "url": url}),
                         daemon=True).start()
