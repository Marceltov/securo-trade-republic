import asyncio
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from trade_republic import client as c


class FakeSocket:
    def __init__(self, replies):
        self.replies = list(replies)  # strings, or a callable(sent) -> str, or an Exception
        self.sent = []
        self.closed = False

    async def send(self, message):
        self.sent.append(message)

    async def recv(self):
        if not self.replies:
            raise ConnectionError("closed")
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply

    async def close(self):
        self.closed = True


def session(socket):
    async def connect(url, **kw):
        session.kw = kw
        return socket

    return c.TrSession("+491701234567", "1234", {"tr_session": "S", "tr_refresh": "R"}, http=object(), connect=connect)


def run(coro):
    return asyncio.run(coro)


class Delta(unittest.TestCase):
    def test_apply_delta(self):
        previous = '{"a":1,"b":2}'
        # keep 5 chars, drop 1, add "9", keep the rest (7)
        self.assertEqual(c.apply_delta(previous, "=5\t-1\t+9\t=7"), '{"a":1,"b":2}'.replace("1", "9", 1))


class Read(unittest.TestCase):
    def test_read_returns_first_answer_and_unsubscribes(self):
        sock = FakeSocket(["connected", '1 A {"items": [1]}'])
        s = session(sock)

        async def go():
            result = await s.read("timelineTransactions", {"after": "x"})
            await s.close()
            return result

        self.assertEqual(run(go()), {"items": [1]})
        self.assertTrue(sock.sent[0].startswith("connect 31 "))
        self.assertEqual(json.loads(sock.sent[1].split(" ", 2)[2]), {"type": "timelineTransactions", "after": "x"})
        self.assertEqual(sock.sent[2], "unsub 1")
        self.assertTrue(sock.closed)
        self.assertIn("tr_session=S", session.kw["additional_headers"]["Cookie"])

    def test_read_skips_other_subscriptions_and_delta(self):
        sock = FakeSocket(["connected", '7 A {"x":1}', '1 A {"n":1}'])
        s = session(sock)
        self.assertEqual(run(s.read("cash")), {"n": 1})

    def test_read_error_frame_raises(self):
        s = session(FakeSocket(["connected", '1 E {"errors":[{"errorCode":"UNAUTHORIZED"}]}']))
        with self.assertRaises(c.BankSyncError) as ctx:
            run(s.read("cash"))
        self.assertIn("cash", str(ctx.exception))

    def test_read_closed_socket_raises(self):
        s = session(FakeSocket(["connected"]))
        with self.assertRaises(c.BankSyncError):
            run(s.read("cash"))

    def test_connect_refused_raises(self):
        s = session(FakeSocket(["nope"]))
        with self.assertRaises(c.BankSyncError):
            run(s.read("cash"))

    def test_read_timeout_raises(self):
        class Slow(FakeSocket):
            async def recv(self):
                if self.replies:
                    return self.replies.pop(0)
                await asyncio.sleep(5)

        s = session(Slow(["connected"]))
        with self.assertRaises(c.BankSyncError):
            run(s.read("cash", timeout=0.05))

    def test_disallowed_subscription_raises(self):
        s = session(FakeSocket(["connected"]))
        with self.assertRaises(ValueError):
            run(s.read("simpleCreateOrder", {"isin": "X"}))

    def test_allowed_subscriptions_exact(self):
        self.assertEqual(
            c.ALLOWED_SUBSCRIPTIONS,
            frozenset({"cash", "compactPortfolioByType", "timelineTransactions", "timelineActivityLog", "timelineDetailV2", "instrument", "ticker"}),
        )


if __name__ == "__main__":
    unittest.main()
