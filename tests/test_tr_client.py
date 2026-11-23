import base64
import json
import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import tr_client as c

PHONE = "+491701234567"
PIN = "9876"


def jwt(exp_offset: float) -> str:
    body = base64.urlsafe_b64encode(json.dumps({"exp": int(time.time() + exp_offset)}).encode()).decode().rstrip("=")
    return f"eyJhbGciOiJub25lIn0.{body}.sig"


class Resp:
    def __init__(self, status=200, body=None, cookies=None):
        self.status_code = status
        self._body = body if body is not None else {}
        self.cookies = cookies or {}

    @property
    def ok(self):
        return self.status_code < 400

    def json(self):
        return self._body


class FakeHttp:
    """Records requests; answers from a script keyed by (method, path prefix)."""

    def __init__(self, script):
        self.script = script  # list of (method, path_prefix, Resp)
        self.calls = []
        self.cookies = {}

    def request(self, method, url, **kw):
        path = url.split("traderepublic.com", 1)[1]
        self.calls.append((method, path, kw))
        for i, (m, prefix, resp) in enumerate(self.script):
            if m == method and path.startswith(prefix):
                if len(self.script) > 1 and sum(1 for s in self.script if s[0] == m and path.startswith(s[1])) > 1:
                    self.script.pop(i)
                self.cookies.update(resp.cookies)
                return resp
        raise AssertionError(f"unexpected request {method} {path}")


def login_script(*statuses, login=None):
    script = [("POST", "/api/v2/auth/web/login", login or Resp(200, {"processId": "p-1", "countdownInSeconds": 120}))]
    for status in statuses:
        script.append(("GET", "/api/v2/auth/web/login/processes/p-1", Resp(200, {"status": status})))
    return script


class NormalizePhone(unittest.TestCase):
    def test_normalize_phone(self):
        for raw in ("0170 1234567", "+49 170 1234567", "00491701234567", "+49 (170) 123-4567"):
            self.assertEqual(c.normalize_phone(raw), PHONE)
        for raw in ("abc", "", "12"):
            with self.assertRaises(c.AuthError):
                c.normalize_phone(raw)


class Login(unittest.TestCase):
    def session(self, script):
        http = FakeHttp(script)
        return c.TrSession(PHONE, PIN, None, http=http, sleep=lambda s: None), http

    def test_login_confirmed(self):
        script = login_script("PENDING", "PENDING", "CONFIRMED")
        script[-1][2].cookies = {"tr_session": jwt(300), "tr_refresh": jwt(86400)}
        s, http = self.session(script)
        s.login(wait_seconds=50)
        self.assertIn("tr_refresh", s.cookies)
        posted = http.calls[0][2]["json"]
        self.assertEqual(posted, {"phoneNumber": PHONE, "pin": PIN})
        self.assertIn("X-TR-Device-Info", http.calls[0][2]["headers"])

    def test_login_timeout_needs_auth(self):
        script = login_script(*["PENDING"] * 100)
        clock = iter(range(0, 1000, 2))
        http = FakeHttp(script)
        s = c.TrSession(PHONE, PIN, None, http=http, sleep=lambda s: None, clock=lambda: next(clock))
        with self.assertRaises(c.NeedsAuth) as ctx:
            s.login(wait_seconds=10)
        self.assertIn("Trade Republic app", str(ctx.exception))
        self.assertNotIn(PHONE, str(ctx.exception))
        self.assertNotIn(PIN, str(ctx.exception))

    def test_login_authenticator_needs_auth(self):
        script = [
            ("POST", "/api/v2/auth/web/login", Resp(200, {"processId": "p-1"})),
            ("GET", "/api/v2/auth/web/login/processes/p-1", Resp(200, {"status": "PENDING", "requiredAction": "AUTHENTICATOR_VERIFICATION"})),
        ]
        s, _ = self.session(script)
        with self.assertRaises(c.NeedsAuth) as ctx:
            s.login(wait_seconds=50)
        self.assertIn("authenticator", str(ctx.exception))

    def test_login_wrong_pin_auth_error(self):
        bad = Resp(401, {"errors": [{"errorCode": "AUTHENTICATION_ERROR"}]})
        s, _ = self.session(login_script(login=bad))
        with self.assertRaises(c.AuthError) as ctx:
            s.login(wait_seconds=50)
        self.assertNotIn(PIN, str(ctx.exception))

    def test_login_rejected_in_app_auth_error(self):
        s, _ = self.session(login_script("PENDING", "REJECTED"))
        with self.assertRaises(c.AuthError):
            s.login(wait_seconds=50)


class Renew(unittest.TestCase):
    def test_renew_expired_refresh_returns_false(self):
        http = FakeHttp([])
        s = c.TrSession(PHONE, PIN, {"tr_refresh": jwt(-10), "tr_session": jwt(-400)}, http=http)
        self.assertFalse(s.renew())
        self.assertEqual(http.calls, [])

    def test_renew_without_cookies_returns_false(self):
        self.assertFalse(c.TrSession(PHONE, PIN, None, http=FakeHttp([])).renew())

    def test_renew_401_returns_false(self):
        http = FakeHttp([("GET", "/api/v1/auth/web/session", Resp(401))])
        s = c.TrSession(PHONE, PIN, {"tr_refresh": jwt(3600)}, http=http)
        self.assertFalse(s.renew())

    def test_renew_ok_updates_tr_session(self):
        new = jwt(300)
        http = FakeHttp([("GET", "/api/v1/auth/web/session", Resp(200, cookies={"tr_session": new}))])
        s = c.TrSession(PHONE, PIN, {"tr_refresh": jwt(3600), "tr_session": jwt(-5)}, http=http)
        self.assertTrue(s.renew())
        self.assertEqual(s.cookies["tr_session"], new)

    def test_cookies_only_known_names(self):
        s = c.TrSession(PHONE, PIN, {"tr_refresh": jwt(3600), "other": "x"}, http=FakeHttp([]))
        self.assertEqual(set(s.cookies), {"tr_refresh"})

    def test_securities_account_number(self):
        http = FakeHttp([("GET", "/api/v1/auth/web/session", Resp(200)), ("GET", "/api/v2/auth/account", Resp(200, {"securitiesAccountNumber": "0123"}))])
        s = c.TrSession(PHONE, PIN, {"tr_refresh": jwt(3600)}, http=http)
        self.assertEqual(s.securities_account_number(), "0123")

    def test_device_id_stable_per_phone(self):
        a = c.TrSession(PHONE, PIN, None, http=FakeHttp([]))
        b = c.TrSession(PHONE, PIN, None, http=FakeHttp([]))
        other = c.TrSession("+491709999999", PIN, None, http=FakeHttp([]))
        self.assertEqual(a.device_id, b.device_id)
        self.assertNotEqual(a.device_id, other.device_id)
        self.assertEqual(len(a.device_id), 128)


if __name__ == "__main__":
    unittest.main()
