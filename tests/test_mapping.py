import sys
import unittest
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import ClassVar

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from trade_republic import client as c
from trade_republic import mapping as m

SALT = "00" * 32


def event(id_, status="EXECUTED", value=-12.5, ts="2026-11-19T10:15:00.000+0000", **kw):
    e = {
        "id": id_,
        "timestamp": ts,
        "title": "Example Shop",
        "subtitle": "Kartentransaktion",
        "amount": {"currency": "EUR", "value": value, "fractionDigits": 2},
        "status": status,
        "eventType": "CARD_TRANSACTION",
        "hidden": False,
        "deleted": False,
    }
    e.update(kw)
    return e


class Transactions(unittest.TestCase):
    def test_executed_and_pending_mapped(self):
        rows = m.transaction_rows([event("a"), event("b", status="PENDING", value=30, title="Jane Doe")])
        self.assertEqual(
            rows[0],
            {
                "external_id": "tr-a",
                "description": "Example Shop",
                "payee": "Example Shop",
                "amount": "12.50",
                "type": "debit",
                "date": "2026-11-19",
                "currency": "EUR",
                "status": "posted",
                "provider_category": "CARD_TRANSACTION",
                "raw_data": {"eventType": "CARD_TRANSACTION", "subtitle": "Kartentransaktion"},
            },
        )
        self.assertEqual((rows[1]["status"], rows[1]["type"], rows[1]["amount"]), ("pending", "credit", "30.00"))

    def test_date_is_berlin_day(self):
        rows = m.transaction_rows([event("a", ts="2026-11-19T23:30:00.000+0000")])
        self.assertEqual(rows[0]["date"], "2026-11-20")

    def test_cancelled_deleted_hidden_skipped(self):
        rows = m.transaction_rows([event("a", status="CANCELED"), event("b", deleted=True), event("c", hidden=True), event("d")])
        self.assertEqual([r["external_id"] for r in rows], ["tr-d"])

    def test_unknown_status_fails(self):
        with self.assertRaises(c.BankSyncError):
            m.transaction_rows([event("a", status="SOMETHING_NEW")])

    def test_missing_amount_fails(self):
        with self.assertRaises(c.BankSyncError):
            m.transaction_rows([event("a", amount=None)])

    def test_duplicate_event_ids_deduplicated(self):
        rows = m.transaction_rows([event("a"), event("a", value=-99)])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["amount"], "12.50")


class Paging(unittest.TestCase):
    def pages(self, pages):
        calls = []

        async def read_page(after):
            calls.append(after)
            return pages[len(calls) - 1]

        return read_page, calls

    def run_paged(self, pages, since=None):
        import asyncio

        read_page, calls = self.pages(pages)
        return asyncio.run(m.paged_events(read_page, since)), calls

    def test_follows_cursor_until_missing(self):
        events, calls = self.run_paged(
            [
                {"items": [event("a")], "cursors": {"after": "c1"}},
                {"items": [event("b")], "cursors": {}},
            ]
        )
        self.assertEqual([e["id"] for e in events], ["a", "b"])
        self.assertEqual(calls, [None, "c1"])

    def test_paging_stops_on_repeated_cursor(self):
        _events, calls = self.run_paged(
            [
                {"items": [event("a")], "cursors": {"after": "c1"}},
                {"items": [event("b")], "cursors": {"after": "c1"}},
                {"items": [event("x")], "cursors": {}},
            ]
        )
        self.assertEqual(calls, [None, "c1"])

    def test_paging_stops_at_since(self):
        events, calls = self.run_paged(
            [
                {"items": [event("a", ts="2026-11-19T10:00:00.000+0000"), event("b", ts="2026-11-01T10:00:00.000+0000")], "cursors": {"after": "c1"}},
                {"items": [event("x", ts="2026-10-01T10:00:00.000+0000")], "cursors": {}},
            ],
            since=date(2026, 11, 10),
        )
        self.assertEqual([e["id"] for e in events], ["a"])
        self.assertEqual(calls, [None])

    def test_page_without_items_fails(self):
        with self.assertRaises(c.BankSyncError):
            self.run_paged([{"cursors": {}}])


class AccountRow(unittest.TestCase):
    def test_account_id_hides_account_number(self):
        row = m.account_row([{"accountNumber": "DE00123456789012345678", "currencyId": "EUR", "amount": 1234.5}], SALT)
        self.assertNotIn("123456789", row["external_id"])
        self.assertEqual((row["type"], row["balance"], row["currency"], row["masked_number"]), ("checking", "1234.50", "EUR", "5678"))
        self.assertEqual(row["institution_name"], "Trade Republic")

    def test_no_cash_account_fails(self):
        with self.assertRaises(c.BankSyncError):
            m.account_row([], SALT)


def position(isin, size="2.5", itype="stock"):
    return {"isin": isin, "netSize": size, "averageBuyIn": "10.0", "instrumentType": itype, "name": "Example " + isin}


class Holdings(unittest.TestCase):
    PRICES: ClassVar[dict] = {"US0000000001": Decimal("100"), "IE0000000002": Decimal("50.5"), "XF0000000003": Decimal("80000")}
    TYPES: ClassVar[dict] = {"US0000000001": "stock", "IE0000000002": "fund", "XF0000000003": "crypto"}

    def test_holdings_from_categories(self):
        portfolio = {
            "categories": [
                {"categoryType": "stocksAndETFs", "positions": [position("US0000000001"), position("IE0000000002", "4")]},
                {"categoryType": "cryptos", "positions": [position("XF0000000003", "0.01", "crypto")]},
            ]
        }
        rows = m.holding_rows(portfolio, self.PRICES, self.TYPES)
        self.assertEqual(
            [(r["isin"], r["type"], r["quantity"], r["current_value"]) for r in rows],
            [("US0000000001", "stock", "2.5", "250.00"), ("IE0000000002", "etf", "4", "202.00"), ("XF0000000003", "crypto", "0.01", "800.00")],
        )
        self.assertEqual(rows[0]["external_id"], "tr-pos-US0000000001")
        self.assertEqual(rows[0]["purchase_price"], "25.00")

    def test_missing_price_fails(self):
        portfolio = {"categories": [{"categoryType": "stocksAndETFs", "positions": [position("US0000000001")]}]}
        with self.assertRaises(c.BankSyncError):
            m.holding_rows(portfolio, {}, self.TYPES)

    def test_empty_depot_reported_empty_only_when_categories_present(self):
        self.assertEqual(m.holding_rows({"categories": [{"categoryType": "stocksAndETFs", "positions": []}]}, {}, {}), [])
        with self.assertRaises(c.BankSyncError):
            m.holding_rows({}, {}, {})


if __name__ == "__main__":
    unittest.main()


ISIN = "XX0000000001"
NB = "\xa0"


def trade_event(id_, event_type="TRADING_SAVINGSPLAN_EXECUTED", value=-50.0, status="EXECUTED", ts="2020-01-02T09:00:00.000+0000"):
    return {
        "id": id_,
        "timestamp": ts,
        "title": "Example ETF",
        "subtitle": "Sparplan ausgeführt",
        "amount": {"currency": "EUR", "value": value, "fractionDigits": 2},
        "status": status,
        "eventType": event_type,
        "hidden": False,
        "deleted": False,
    }


def header(isin=ISIN):
    return {"title": "Du hast 50,00 € gespart", "type": "header", "action": {"type": "instrumentDetail", "payload": isin}, "data": {"status": "executed"}}


def row(title, text):
    return {"title": title, "style": "plain", "detail": {"type": "text", "text": text}}


DOCUMENTS = {
    "title": "Dokumente",
    "type": "documents",
    "data": [
        {
            "title": "Abrechnung",
            "detail": "02.01.2020",
            "id": "doc1",
            "postboxType": "SECURITIES_SETTLEMENT",
            "action": {"type": "browserModal", "payload": "https://example.invalid/doc.pdf"},
        }
    ],
}


def new_detail(units, price, fee="Kostenlos", isin=ISIN):
    """The layout of TRADING_* events: units and price on the info page behind the Transaktion row."""
    info = {
        "sections": [
            {"title": "Transaktion", "type": "header", "data": {"status": "executed"}},
            {"type": "table", "data": [row("Aktienkurs", f"{price}{NB}€"), row("Aktien", units), row("Summe", f"50,00{NB}€")]},
        ]
    }
    transaktion = {
        "title": "Transaktion",
        "style": "plain",
        "detail": {"type": "text", "text": f"{units} ×  {price}{NB}€", "action": {"type": "infoPage", "payload": info}},  # noqa: RUF001, Trade Republic writes a multiplication sign
    }
    return {
        "id": "d",
        "sections": [
            header(isin),
            {"title": "Übersicht", "type": "table", "data": [row("Sparplan", "Ausgeführt"), transaktion, row("Gebühr", fee), row("Summe", f"50,00{NB}€")]},
            DOCUMENTS,
        ],
    }


def old_detail(units, price, fee, isin=ISIN):
    """The layout of ORDER_EXECUTED: its own Transaktion table."""
    return {
        "id": "d",
        "sections": [
            header(isin),
            {"title": "Übersicht", "type": "table", "data": [row("Status", "Ausgeführt"), row("Orderart", "Kauf")]},
            {
                "title": "Transaktion",
                "type": "table",
                "data": [row("Anteile", units), row("Aktienkurs", f"{price}{NB}€"), row("Gebühr", f"{fee}{NB}€"), row("Gesamt", f"101,00{NB}€")],
            },
            DOCUMENTS,
        ],
    }


class Trades(unittest.TestCase):
    def test_order_becomes_a_buy_on_its_execution_day(self):
        trades = m.trade_rows([trade_event("o", "ORDER_EXECUTED", -101.0)], {"o": old_detail("2", "50,00", "1,00")}, SALT)
        self.assertEqual(list(trades), [ISIN])
        t = trades[ISIN][0]
        self.assertEqual(
            {k: t[k] for k in ("kind", "date", "quantity", "price", "fee")},
            {"kind": "buy", "date": "2020-01-02", "quantity": "2", "price": "50", "fee": "1.00"},
        )

    def test_savings_plan_run_is_a_buy_without_fee(self):
        t = m.trade_rows([trade_event("s")], {"s": new_detail("0,5", "100,00")}, SALT)[ISIN][0]
        self.assertEqual((t["kind"], t["quantity"], t["price"], t["fee"]), ("buy", "0.5", "100", "0.00"))

    def test_sell_is_a_sell_with_positive_units(self):
        t = m.trade_rows([trade_event("x", "TRADING_TRADE_EXECUTED", 300.0)], {"x": new_detail("3", "100,00", f"1,00{NB}€")}, SALT)[ISIN][0]
        self.assertEqual((t["kind"], t["quantity"], t["fee"]), ("sell", "3", "1.00"))

    def test_cancelled_order_is_not_a_trade(self):
        self.assertEqual(m.trade_rows([trade_event("c", "ORDER_EXECUTED", status="CANCELED")], {}, SALT), {})

    def test_failed_savings_plan_run_is_not_a_trade(self):
        self.assertEqual(m.trade_rows([trade_event("f", "TRADING_SAVINGSPLAN_EXECUTION_FAILED")], {}, SALT), {})

    def test_crypto_units_keep_six_decimals(self):
        t = m.trade_rows([trade_event("b")], {"b": new_detail("0,0012345678", "1.234,56")}, SALT)[ISIN][0]
        self.assertEqual((t["quantity"], t["price"]), ("0.001235", "1234.56"))

    def test_unknown_unit_event_fails_naming_it(self):
        with self.assertRaises(c.BankSyncError) as caught:
            m.trade_rows([trade_event("t", "SSP_SECURITIES_TRANSFER_OUTGOING", 0.0)], {}, SALT)
        self.assertIn("SSP_SECURITIES_TRANSFER_OUTGOING", str(caught.exception))

    def test_trade_detail_without_units_fails(self):
        detail = old_detail("2", "50,00", "1,00")
        detail["sections"][2]["data"] = [r for r in detail["sections"][2]["data"] if r["title"] != "Anteile"]
        with self.assertRaises(c.BankSyncError):
            m.trade_rows([trade_event("o", "ORDER_EXECUTED")], {"o": detail}, SALT)

    def test_trade_detail_without_isin_fails(self):
        detail = new_detail("1", "10,00")
        detail["sections"][0]["action"] = None
        with self.assertRaises(c.BankSyncError):
            m.trade_rows([trade_event("s")], {"s": detail}, SALT)

    def test_external_id_is_stable_and_salted(self):
        def ids(salt):
            return m.trade_rows([trade_event("s")], {"s": new_detail("1", "10,00")}, salt)[ISIN][0]["external_id"]

        self.assertEqual(ids(SALT), ids(SALT))
        self.assertNotEqual(ids(SALT), ids("11" * 32))
        self.assertNotIn("s", ids(SALT).removeprefix("tr-trade-"))

    def test_execution_day_is_berlin_local(self):
        t = m.trade_rows([trade_event("s", ts="2020-01-01T23:30:00.000+0000")], {"s": new_detail("1", "10,00")}, SALT)[ISIN][0]
        self.assertEqual(t["date"], "2020-01-02")

    def test_trades_of_two_positions_are_kept_apart(self):
        trades = m.trade_rows([trade_event("a"), trade_event("b")], {"a": new_detail("1", "10,00"), "b": new_detail("2", "20,00", isin="XX0000000002")}, SALT)
        self.assertEqual(sorted(trades), [ISIN, "XX0000000002"])

    def test_a_detail_without_a_fee_row_has_no_fee(self):
        detail = new_detail("1", "10,00")
        detail["sections"][1]["data"] = [r for r in detail["sections"][1]["data"] if r["title"] != "Gebühr"]
        self.assertEqual(m.trade_rows([trade_event("s")], {"s": detail}, SALT)[ISIN][0]["fee"], "0.00")

    def test_known_trades_need_no_detail(self):
        known = m.trade_rows([trade_event("s")], {"s": new_detail("1", "10,00")}, SALT)
        again = m.trade_rows([trade_event("s")], {}, SALT, known=m.remembered(known))
        self.assertEqual(again, known)

    def test_saveback_is_a_buy_whatever_its_sign(self):
        t = m.trade_rows([trade_event("sb", "BENEFITS_SAVEBACK_EXECUTION", 15.0)], {"sb": new_detail("0,1", "150,00")}, SALT)[ISIN][0]
        self.assertEqual(t["kind"], "buy")

    def test_hidden_trade_event_is_not_a_trade(self):
        e = trade_event("h")
        e["hidden"] = True
        self.assertEqual(m.trade_rows([e], {}, SALT), {})

    def test_units_are_rounded_so_the_total_stays_exact(self):
        events = [trade_event(f"r{i}", ts=f"2020-01-0{i + 1}T09:00:00.000+0000") for i in range(3)]
        details = {f"r{i}": new_detail("0,3333333", "10,00") for i in range(3)}
        rows = m.trade_rows(events, details, SALT)[ISIN]
        self.assertEqual(sum(Decimal(t["quantity"]) for t in rows), Decimal("1.000000"))
        self.assertTrue(all(Decimal(t["quantity"]) > 0 for t in rows))

    def test_broken_remembered_trades_are_dropped(self):
        good = m.remembered(m.trade_rows([trade_event("s")], {"s": new_detail("1", "10,00")}, SALT))
        ((key, entry),) = good.items()
        known = {
            key: entry,
            "x1": {**entry, "kind": "gift"},
            "x2": {**entry, "quantity": "many"},
            "x3": "nonsense",
            "x4": {**entry, "isin": ""},
            "x5": {**entry, "date": "yesterday"},
        }
        self.assertEqual(m.valid_known(known), {key: entry})


class ClosedPositions(unittest.TestCase):
    def test_a_position_no_longer_held_comes_in_closed_with_its_trades(self):
        trades = m.trade_rows(
            [trade_event("b", value=-20.0), trade_event("s", "TRADING_TRADE_EXECUTED", 25.0)],
            {"b": new_detail("2", "10,00"), "s": new_detail("2", "12,50")},
            SALT,
        )
        events = [trade_event("b")]
        rows = m.closed_holding_rows(trades, events, SALT, held=set())
        self.assertEqual(len(rows), 1)
        r = rows[0]
        self.assertEqual(
            {k: r[k] for k in ("external_id", "name", "quantity", "current_value", "is_withdrawn", "isin")},
            {"external_id": f"tr-pos-{ISIN}", "name": "Example ETF", "quantity": "0", "current_value": "0.00", "is_withdrawn": True, "isin": ISIN},
        )
        self.assertEqual(len(r["trades"]), 2)

    def test_a_held_position_is_not_reported_closed(self):
        trades = m.trade_rows([trade_event("b")], {"b": new_detail("2", "10,00")}, SALT)
        self.assertEqual(m.closed_holding_rows(trades, [trade_event("b")], SALT, held={ISIN}), [])


def activity_event(id_, event_type, isin=ISIN, ts="2020-03-01T09:00:00.000+0000", status="EXECUTED"):
    return {
        "id": id_,
        "timestamp": ts,
        "title": "Example ETF",
        "subtitle": "Aktien erhalten",
        "eventType": event_type,
        "status": status,
        "icon": f"logos/{isin}/v2",
        "hidden": False,
        "deleted": False,
    }


def transfer_detail(units):
    return {
        "id": "d",
        "sections": [
            {"title": f"Du hast {units} Aktien erhalten", "type": "header", "data": {"icon": "logos/x/v2"}},
            {"title": "Überblick", "type": "table", "data": [row("Status", "Abgeschlossen"), row("Aktion", "Übertragung von Wertpapieren")]},
            {"title": "Transaktion", "type": "table", "data": [row("Aktien", units), row("Gebühr", "Kostenlos")]},
        ],
    }


def perk_detail(units, price, isin=ISIN):
    return {
        "id": "d",
        "sections": [
            {"title": "Gratisaktien eingelöst", "type": "header", "action": {"type": "instrumentDetail", "payload": isin}},
            {"title": "Transaktion", "type": "table", "data": [row("Anteile", units), row("Aktienkurs", f"{price}{NB}€")]},
            DOCUMENTS,
        ],
    }


class Activity(unittest.TestCase):
    def test_numbers_with_a_decimal_point_are_read_as_decimals(self):
        self.assertEqual(m._detail_number("0.022", "x"), Decimal("0.022"))
        self.assertEqual(m._detail_number("0.0068", "x"), Decimal("0.0068"))
        self.assertEqual(m._detail_number("1.234", "x"), Decimal("1234"))
        self.assertEqual(m._detail_number(f"1.234,56{NB}€", "x"), Decimal("1234.56"))

    def test_free_shares_are_a_buy_at_their_price(self):
        t = m.trade_rows([activity_event("p", "STOCK_PERK_REFUNDED")], {"p": perk_detail("0.022", "222,80")}, SALT)[ISIN][0]
        self.assertEqual((t["kind"], t["quantity"], t["price"], t["date"]), ("buy", "0.022", "222.8", "2020-03-01"))

    def test_a_transfer_in_is_a_buy_found_by_its_icon_and_priced_later(self):
        t = m.trade_rows([activity_event("t", "SSP_SECURITIES_TRANSFER_INCOMING")], {"t": transfer_detail("4")}, SALT)[ISIN][0]
        self.assertEqual((t["kind"], t["quantity"], t["price"]), ("buy", "4", None))

    def test_a_transfer_is_priced_from_the_average_buy_in(self):
        trades = m.trade_rows(
            [trade_event("b", value=-20.0), activity_event("t", "SSP_SECURITIES_TRANSFER_INCOMING")],
            {"b": new_detail("2", "10,00"), "t": transfer_detail("4")},
            SALT,
        )
        # 6 units held at an average of 12: 72 invested, 20 of it by the buy -> 52 / 4 = 13 per transferred unit
        m.price_transfers(trades, {ISIN: (Decimal("6"), Decimal("12"))})
        transfer = next(t for t in trades[ISIN] if t["quantity"] == "4")
        self.assertEqual(transfer["price"], "13")

    def test_a_closed_position_that_does_not_add_up_is_left_out(self):
        trades = m.trade_rows(
            [activity_event("p", "STOCK_PERK_REFUNDED"), trade_event("s", "TRADING_TRADE_EXECUTED", 5.0)],
            {"p": perk_detail("0.0068", "778,00"), "s": new_detail("0,0204", "250,00")},
            SALT,
        )
        self.assertEqual(m.closed_holding_rows(trades, [], SALT, held=set()), [])
