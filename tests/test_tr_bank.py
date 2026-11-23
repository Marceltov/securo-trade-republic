import asyncio
import json
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import tr_bank as b
import tr_client as c

PHONE, PIN = "+491701234567", "9876"
SALT = "ab" * 32
COOKIES = {"tr_refresh": "REFRESHTOKENVALUE", "tr_session": "SESSIONTOKENVALUE"}

READS = {
    "cash": [{"accountNumber": "DE00123456789012345678", "currencyId": "EUR", "amount": 100.0}],
    "compactPortfolioByType": {
        "categories": [
            {
                "categoryType": "stocksAndETFs",
                "positions": [{"isin": "US0000000001", "netSize": "2", "averageBuyIn": "10", "instrumentType": "stock", "name": "Example"}],
            }
        ]
    },
    "instrument": {"exchangeIds": ["LSX"], "typeId": "stock"},
    "ticker": {"last": {"price": "12.5"}},
    "timelineTransactions": {
        "items": [
            {
                "id": "e1",
                "timestamp": "2026-11-19T10:00:00.000+0000",
                "title": "Example Shop",
                "amount": {"currency": "EUR", "value": -5},
                "status": "EXECUTED",
                "eventType": "CARD_TRANSACTION",
            }
        ],
        "cursors": {},
    },
    "timelineActivityLog": {"items": [], "cursors": {}},
    "details": {
        "base": {
            "id": "base",
            "sections": [
                {"title": "Du hast 20,00 € investiert", "type": "header", "action": {"type": "instrumentDetail", "payload": "US0000000001"}},
                {
                    "title": "Transaktion",
                    "type": "table",
                    "data": [
                        {"title": "Anteile", "detail": {"type": "text", "text": "2"}},
                        {"title": "Aktienkurs", "detail": {"type": "text", "text": "10,00\xa0€"}},
                    ],
                },
            ],
        }
    },
}
READS["timelineTransactions"]["items"].append(
    {
        "id": "base",
        "timestamp": "2020-01-02T09:00:00.000+0000",
        "title": "Example",
        "amount": {"currency": "EUR", "value": -20.0},
        "status": "EXECUTED",
        "eventType": "ORDER_EXECUTED",
    }
)


class FakeSession:
    instances = []

    def __init__(self, phone, pin, cookies, renews=True, login_error=None, read_error=None):
        self.phone, self.pin = phone, pin
        self._cookies = dict(cookies or {})
        self.renews, self.login_error, self.read_error = renews, login_error, read_error
        self.logged_in = False
        self.reads = []
        FakeSession.instances.append(self)

    @property
    def cookies(self):
        return dict(self._cookies)

    def renew(self):
        if isinstance(self.renews, Exception):
            raise self.renews
        return self.renews and bool(self._cookies)

    def login(self, wait_seconds=50):
        assert wait_seconds == 50
        if self.login_error:
            raise self.login_error
        self.logged_in = True
        self._cookies = {"tr_refresh": "NEWREFRESH", "tr_session": "NEWSESSION"}

    def securities_account_number(self):
        return "0123"

    async def read(self, kind, payload=None, timeout=20):
        self.reads.append((kind, payload))
        if self.read_error:
            raise self.read_error
        if kind == "timelineDetailV2":
            return READS["details"][payload["id"]]
        return READS[kind]

    async def close(self):
        pass


def context(secrets=None):
    base = {"phone": "0170 1234567", "pin": PIN, "id_salt": SALT, "session": json.dumps(COOKIES)}
    return SimpleNamespace(secrets=base if secrets is None else secrets, config={}, securo={})


def sync(params=None, ctx=None, clock=None, **session_kw):
    FakeSession.instances.clear()
    factory = lambda phone, pin, cookies: FakeSession(phone, pin, cookies, **session_kw)
    plugin = b.Plugin(session_factory=factory, **({"clock": clock} if clock else {}))
    return asyncio.run(plugin.sync(params or {}, ctx or context()))


class Sync(unittest.TestCase):
    def test_reuses_session_no_login(self):
        result = sync()
        s = FakeSession.instances[0]
        self.assertFalse(s.logged_in)
        self.assertEqual(s.phone, PHONE)
        self.assertEqual(json.loads(result["secrets"]["session"]), COOKIES)

    def test_expired_session_logs_in_and_returns_new_cookies(self):
        result = sync(renews=False)
        self.assertTrue(FakeSession.instances[0].logged_in)
        self.assertEqual(json.loads(result["secrets"]["session"])["tr_refresh"], "NEWREFRESH")

    def test_renew_rejected_falls_back_to_login(self):
        sync(ctx=context({"phone": PHONE, "pin": PIN, "id_salt": SALT}))
        self.assertTrue(FakeSession.instances[0].logged_in)

    def test_login_timeout_is_needs_auth(self):
        with self.assertRaises(c.NeedsAuth):
            sync(renews=False, login_error=c.NeedsAuth("confirm the login in the Trade Republic app, then press Sync again"))

    def test_missing_credentials_needs_auth(self):
        with self.assertRaises(b.NeedsAuth):
            sync(ctx=context({}))

    def test_ws_error_fails_without_data(self):
        with self.assertRaises(c.BankSyncError):
            sync(read_error=c.BankSyncError("Trade Republic refused cash"))

    def test_unexpected_exception_becomes_bank_sync_error(self):
        with self.assertRaises(b.BankSyncError) as ctx:
            sync(renews=ConnectionError(f"boom {PHONE} {PIN} REFRESHTOKENVALUE"))
        self.assertIn("ConnectionError", str(ctx.exception))

    def test_error_messages_never_contain_secrets(self):
        for kw in (
            {"renews": ConnectionError(f"x {PHONE} {PIN} REFRESHTOKENVALUE")},
            {"read_error": RuntimeError(f"y {PIN} SESSIONTOKENVALUE DE00123456789012345678")},
        ):
            with self.assertRaises(Exception) as ctx:
                sync(**kw)
            for secret in (PHONE, PIN, "REFRESHTOKENVALUE", "SESSIONTOKENVALUE", "123456789012345678"):
                self.assertNotIn(secret, str(ctx.exception))

    def test_payload_shape_contract_1(self):
        result = sync()
        data = result["data"]
        self.assertEqual(data["contract"], 1)
        self.assertEqual(len(data["accounts"]), 1)
        self.assertEqual(data["accounts"][0]["transactions"][0]["external_id"], "tr-e1")
        self.assertEqual(data["holdings"][0]["current_value"], "25.00")
        self.assertNotIn("account_external_id", data["holdings"][0])
        self.assertEqual(result["secrets"]["id_salt"], SALT)

    def test_ticker_uses_first_exchange(self):
        sync()
        reads = FakeSession.instances[0].reads
        self.assertIn(("ticker", {"id": "US0000000001.LSX"}), reads)
        self.assertIn(("compactPortfolioByType", {"secAccNo": "0123"}), reads)

    def test_since_passed_to_timeline(self):
        sync({"since": "2026-11-25"})
        # the only event (2026-11-19) is older than since
        # -> no transactions, but the account is still reported
        result = sync({"since": "2026-11-25"})
        self.assertEqual(result["data"]["accounts"][0]["transactions"], [])

    def test_new_salt_created_once(self):
        result = sync(ctx=context({"phone": PHONE, "pin": PIN, "session": json.dumps(COOKIES)}))
        self.assertEqual(len(result["secrets"]["id_salt"]), 64)


if __name__ == "__main__":
    unittest.main()


from tests.test_tr_map import old_detail, trade_event  # noqa: E402

POS = "US0000000001"


class with_timeline:
    """READS with extra timeline events and their details, restored afterwards."""

    def __init__(self, events, details):
        self.events, self.details = events, details

    def __enter__(self):
        self.saved = (READS["timelineTransactions"], READS["details"])
        items = [e for e in self.saved[0]["items"] if e.get("eventType") != "ORDER_EXECUTED"]
        READS["timelineTransactions"] = {"items": items + self.events, "cursors": {}}
        READS["details"] = self.details

    def __exit__(self, *exc):
        READS["timelineTransactions"], READS["details"] = self.saved


class Trades(unittest.TestCase):
    def _two_buys(self):
        return with_timeline(
            [
                trade_event("t1", "ORDER_EXECUTED", -11.0, ts="2026-10-01T09:00:00.000+0000"),
                trade_event("t2", "TRADING_SAVINGSPLAN_EXECUTED", -10.0, ts="2020-05-04T09:00:00.000+0000"),
            ],
            {"t1": old_detail("1,5", "7,00", "1,00", isin=POS), "t2": old_detail("0,5", "20,00", "Kostenlos", isin=POS)},
        )

    def test_sync_attaches_trades_to_their_holding(self):
        with self._two_buys():
            holding = sync()["data"]["holdings"][0]
        self.assertEqual(sorted((t["date"], t["kind"], t["quantity"]) for t in holding["trades"]), [("2020-05-04", "buy", "0.5"), ("2026-10-01", "buy", "1.5")])

    def test_sync_reads_the_whole_timeline_for_trades(self):
        with self._two_buys():
            result = sync({"since": "2026-09-01"})
        self.assertIn("2020-05-04", [t["date"] for t in result["data"]["holdings"][0]["trades"]])
        self.assertNotIn("2020-05-04", [t["date"] for t in result["data"]["accounts"][0]["transactions"]])

    def test_sync_reads_details_only_for_trade_events(self):
        with self._two_buys():
            sync()
        detail_reads = [p["id"] for kind, p in FakeSession.instances[-1].reads if kind == "timelineDetailV2"]
        self.assertEqual(sorted(detail_reads), ["t1", "t2"])

    def test_trades_that_do_not_add_up_fail_naming_the_position(self):
        with with_timeline([trade_event("t1", "ORDER_EXECUTED", -11.0)], {"t1": old_detail("1,5", "7,00", "1,00", isin=POS)}):
            with self.assertRaises(c.BankSyncError) as caught:
                sync()
        self.assertIn(POS, str(caught.exception))

    def test_a_position_without_any_trade_fails(self):
        with with_timeline([], {}):
            with self.assertRaises(c.BankSyncError):
                sync()

    def test_a_slow_first_sync_leaves_trades_out_and_keeps_what_it_read(self):
        ticks = iter([0.0] + [100.0] * 50)
        with self._two_buys():
            first = sync(clock=lambda: next(ticks))
            self.assertNotIn("trades", first["data"]["holdings"][0])
            self.assertEqual(len(first["state"]["trades"]), 1)
            second = sync({"state": first["state"]})
        self.assertEqual(len(second["data"]["holdings"][0]["trades"]), 2)
        detail_reads = [p for kind, p in FakeSession.instances[-1].reads if kind == "timelineDetailV2"]
        self.assertEqual(len(detail_reads), 1)

    def test_a_later_sync_reads_only_new_trade_details(self):
        with self._two_buys():
            first = sync()
            second = sync({"state": first["state"]})
        detail_reads = [p for kind, p in FakeSession.instances[-1].reads if kind == "timelineDetailV2"]
        self.assertEqual(detail_reads, [])
        self.assertEqual(
            sorted(t["external_id"] for t in second["data"]["holdings"][0]["trades"]), sorted(t["external_id"] for t in first["data"]["holdings"][0]["trades"])
        )

    def test_remembered_trades_hold_no_event_ids(self):
        with self._two_buys():
            state = sync()["state"]
        self.assertNotIn("t1", json.dumps(state))

    def test_a_sold_out_position_comes_in_closed(self):
        gone = "XX0000000009"
        with with_timeline(
            [trade_event("t0", "ORDER_EXECUTED", -20.0), trade_event("g1", "ORDER_EXECUTED", -30.0), trade_event("g2", "TRADING_TRADE_EXECUTED", 40.0)],
            {"t0": old_detail("2", "10,00", "0", isin=POS), "g1": old_detail("3", "10,00", "0", isin=gone), "g2": old_detail("3", "13,00", "0", isin=gone)},
        ):
            holdings = sync()["data"]["holdings"]
        closed = [h for h in holdings if h["isin"] == gone]
        self.assertEqual(len(closed), 1)
        self.assertTrue(closed[0]["is_withdrawn"])
        self.assertEqual(sorted(t["kind"] for t in closed[0]["trades"]), ["buy", "sell"])

    def test_deleted_trade_events_are_not_read(self):
        e = trade_event("del", "ORDER_EXECUTED")
        e["deleted"] = True
        with with_timeline([trade_event("t0", "ORDER_EXECUTED", -20.0), e], {"t0": old_detail("2", "10,00", "0", isin=POS)}):
            sync()
        self.assertNotIn({"id": "del"}, [p for kind, p in FakeSession.instances[-1].reads if kind == "timelineDetailV2"])

    def test_a_transfer_in_from_the_activity_log_makes_a_position_add_up(self):
        from tests.test_tr_map import activity_event, transfer_detail

        saved = READS.get("timelineActivityLog")
        READS["timelineActivityLog"] = {"items": [activity_event("tr", "SSP_SECURITIES_TRANSFER_INCOMING", isin=POS)], "cursors": {}}
        try:
            with with_timeline([trade_event("t0", "ORDER_EXECUTED", -10.0)], {"t0": old_detail("1", "10,00", "0", isin=POS), "tr": transfer_detail("1")}):
                holding = sync()["data"]["holdings"][0]
        finally:
            READS["timelineActivityLog"] = saved
        self.assertEqual(sorted(t["quantity"] for t in holding["trades"]), ["1", "1"])
        self.assertTrue(all(t["price"] is not None for t in holding["trades"]))
