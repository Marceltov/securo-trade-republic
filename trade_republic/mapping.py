"""Trade Republic answers as Securo contract 1 records.

Anything this module does not recognise stops the sync with BankSyncError: an
unexpected answer must never become an empty list, which Securo would read as
"everything is gone" and archive.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import re
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from zoneinfo import ZoneInfo

from trade_republic.client import BankSyncError

BERLIN = ZoneInfo("Europe/Berlin")
CENT = Decimal("0.01")
# Timeline statuses: what each becomes in Securo. None: not reported.
STATUSES = {"EXECUTED": "posted", "PENDING": "pending", "CANCELED": None, "CANCELLED": None}
HOLDING_TYPES = {"stock": "stock", "fund": "etf", "crypto": "crypto", "bond": "investment", "derivative": "investment"}
MAX_PAGES = 1000


def _digest(salt: str, prefix: str, *values) -> str:
    content = "\x1f".join(str(v) for v in values).encode()
    return prefix + hmac.new(bytes.fromhex(salt), content, hashlib.sha256).hexdigest()[:40]


def _decimal(value, what: str) -> Decimal:
    try:
        number = Decimal(str(value))
    except InvalidOperation, TypeError, ValueError:
        raise BankSyncError(f"Trade Republic sent an unreadable {what}; nothing was imported") from None
    if not number.is_finite():
        raise BankSyncError(f"Trade Republic sent an invalid {what}; nothing was imported")
    return number


def _money(value: Decimal) -> str:
    return str(value.quantize(CENT, rounding=ROUND_HALF_UP))


def _day(timestamp) -> dt.date:
    try:
        moment = dt.datetime.strptime(str(timestamp), "%Y-%m-%dT%H:%M:%S.%f%z")
    except ValueError:
        try:
            moment = dt.datetime.fromisoformat(str(timestamp))
        except ValueError:
            raise BankSyncError("Trade Republic sent an event without a readable date; nothing was imported") from None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=dt.UTC)
    return moment.astimezone(BERLIN).date()


def _text(value, size: int) -> str:
    return " ".join(str(value or "").split())[:size]


def account_row(cash: list, salt: str) -> dict:
    if not isinstance(cash, list) or len(cash) != 1 or not isinstance(cash[0], dict):
        raise BankSyncError("Trade Republic returned no single cash account; nothing was imported")
    entry = cash[0]
    number = str(entry.get("accountNumber") or "")
    currency = str(entry.get("currencyId") or "")
    if not number or len(currency) != 3:
        raise BankSyncError("Trade Republic returned an incomplete cash account; nothing was imported")
    return {
        "external_id": _digest(salt, "tr-acct-", number),
        "name": "Trade Republic",
        "type": "checking",
        "balance": _money(_decimal(entry.get("amount"), "balance")),
        "currency": currency.upper(),
        "masked_number": number[-4:],
        "institution_name": "Trade Republic",
    }


def transaction_rows(events: list[dict]) -> list[dict]:
    rows, seen = [], set()
    for e in events:
        if not isinstance(e, dict) or not e.get("id"):
            raise BankSyncError("Trade Republic sent a timeline event without an id; nothing was imported")
        if e["id"] in seen:
            continue
        seen.add(e["id"])
        status = e.get("status")
        if status not in STATUSES:
            raise BankSyncError(f"Trade Republic sent an unknown event status ({_text(status, 30)}); nothing was imported")
        if STATUSES[status] is None or e.get("deleted") or e.get("hidden"):
            continue
        amount = e.get("amount")
        if not isinstance(amount, dict) or amount.get("value") is None:
            raise BankSyncError("Trade Republic sent an event without an amount; nothing was imported")
        value = _decimal(amount["value"], "amount")
        title = _text(e.get("title"), 200) or "Trade Republic"
        rows.append(
            {
                "external_id": f"tr-{_text(e['id'], 120)}",
                "description": title,
                "payee": title,
                "amount": _money(abs(value)),
                "type": "debit" if value < 0 else "credit",
                "date": _day(e.get("timestamp")).isoformat(),
                "currency": str(amount.get("currency") or "EUR").upper(),
                "status": STATUSES[status],
                "provider_category": _text(e.get("eventType"), 100) or None,
                "raw_data": {"eventType": e.get("eventType"), "subtitle": _text(e.get("subtitle"), 200)},
            }
        )
    return rows


async def paged_events(read_page, since: dt.date | None) -> list[dict]:
    """All timeline events (newest first) down to `since`, following the `after` cursor."""
    events, cursor, used = [], None, set()
    for _ in range(MAX_PAGES):
        page = await read_page(cursor)
        items = page.get("items") if isinstance(page, dict) else None
        if not isinstance(items, list):
            raise BankSyncError("Trade Republic sent an unreadable timeline page; nothing was imported")
        for item in items:
            if since is not None and _day(item.get("timestamp")) < since:
                return events
            events.append(item)
        cursor = (page.get("cursors") or {}).get("after")
        if not cursor or cursor in used:
            return events
        used.add(cursor)
    raise BankSyncError("the Trade Republic timeline did not end; nothing was imported")


# Timeline events that move units: each becomes a buy or sell on its position.
TRADE_EVENTS = frozenset(
    {
        "ORDER_EXECUTED",
        "TRADING_SAVINGSPLAN_EXECUTED",
        "TRADING_TRADE_EXECUTED",
        "TRADE_INVOICE",
        "SAVINGS_PLAN_EXECUTED",
        "SAVINGS_PLAN_INVOICE_CREATED",
        "TRADE_CORRECTED",
        "BENEFITS_SAVEBACK_EXECUTION",
        "BENEFITS_SPARE_CHANGE_EXECUTION",
        "SAVEBACK_AGGREGATE",
        "SPARE_CHANGE_AGGREGATE",
    }
)
# Events that move units in a way not mapped yet: refusing them keeps the
# history from silently not adding up.
UNIT_EVENTS_REFUSED = frozenset(
    {
        "SSP_SECURITIES_TRANSFER_OUTGOING",
        "ACQUISITION_TRADE_PERK",
        "IPO_SUBSCRIPTION_ALLOCATION",
        "IPO_TRADE_EXECUTED",
        "SSP_CORPORATE_ACTION_INVOICE_SHARES",
    }
)
# Activity log entries that bring units in: a transfer from another bank
# (no price: see price_transfers) and free shares Trade Republic gave.
TRANSFER_IN = "SSP_SECURITIES_TRANSFER_INCOMING"
ACTIVITY_TRADE_EVENTS = frozenset({TRANSFER_IN, "STOCK_PERK_REFUNDED"})
ISIN_IN_ICON = re.compile(r"logos/([A-Z]{2}[A-Z0-9]{9}[0-9])/")
# Bought for you (Saveback, round-ups): a buy whatever the sign of its amount.
BUY_ONLY_EVENTS = (
    frozenset({"BENEFITS_SAVEBACK_EXECUTION", "SAVEBACK_AGGREGATE", "BENEFITS_SPARE_CHANGE_EXECUTION", "SPARE_CHANGE_AGGREGATE"}) | ACTIVITY_TRADE_EVENTS
)
MICRO = Decimal("0.000001")
FREE = {"kostenlos", "gratis", "free"}


def _detail_number(text, what: str) -> Decimal:
    """A number as the detail pages show it: "1.234,56 €", "0,001234"."""
    cleaned = str(text or "").replace("\xa0", " ").replace("€", "").strip()
    if cleaned.lower() in FREE:
        return Decimal(0)
    if "," in cleaned:  # German: "1.234,56"
        return _decimal(cleaned.replace(".", "").replace(",", "."), what)
    if re.fullmatch(r"[1-9][0-9]{0,2}(\.[0-9]{3})+", cleaned):  # "1.234": thousands
        return _decimal(cleaned.replace(".", ""), what)
    return _decimal(cleaned, what)  # "0.022": free shares write a decimal point


def _rows(detail: dict) -> dict[str, str]:
    """Every titled text row of the detail, the Transaktion info page included."""
    found: dict[str, str] = {}

    def walk(sections):
        for section in sections if isinstance(sections, list) else []:
            data = section.get("data") if isinstance(section, dict) else None
            for r in data if isinstance(data, list) else []:
                # A row's detail is usually an object; a document row's is plain text.
                detail_of_row = r.get("detail") if isinstance(r, dict) else None
                if not isinstance(detail_of_row, dict):
                    continue
                info = detail_of_row.get("action")
                if isinstance(info, dict) and isinstance(info.get("payload"), dict):
                    walk(info["payload"].get("sections"))
                text = detail_of_row.get("text")
                if isinstance(text, str):
                    found.setdefault(str(r.get("title") or ""), text)

    walk(detail.get("sections") if isinstance(detail, dict) else None)
    return found


def _isin(detail: dict, event: dict | None = None) -> str:
    """The security of a detail: its header's instrument link, else the event's logo (logos/<ISIN>/v2)."""
    found = _header_isin(detail)
    if not found and isinstance(event, dict):
        match = ISIN_IN_ICON.search(str(event.get("icon") or ""))
        found = match.group(1) if match else ""
    return found


def _header_isin(detail: dict) -> str:
    for section in detail.get("sections") or [] if isinstance(detail, dict) else []:
        action = section.get("action") if isinstance(section, dict) else None
        if section.get("type") == "header" and isinstance(action, dict) and action.get("type") == "instrumentDetail":
            return str(action.get("payload") or "")
    return ""


def trade_id(salt: str, event_id) -> str:
    return _digest(salt, "tr-trade-", event_id)


def remembered(trades: dict[str, list[dict]]) -> dict[str, dict]:
    """`trades` keyed by their external id, each with its ISIN: kept in the connection's state."""
    return {t["external_id"]: {**t, "isin": isin} for isin, rows in trades.items() for t in rows}


def trade_rows(events: list[dict], details: dict[str, dict], salt: str, known: dict[str, dict] | None = None) -> dict[str, list[dict]]:
    """The buys and sells in `events`, by ISIN; `details[id]` is each trade event's timelineDetailV2.

    A trade in `known` (see `remembered`) needs no detail: an executed trade
    does not change, and a correction arrives as an event of its own.
    """
    trades: dict[str, list[dict]] = {}
    known = known or {}
    seen = set()
    for e in events:
        kind_of_event = e.get("eventType") if isinstance(e, dict) else None
        if kind_of_event in UNIT_EVENTS_REFUSED:
            raise BankSyncError(f"Trade Republic sent a {_text(kind_of_event, 60)} event, which this plugin cannot map yet; nothing was imported")
        if (
            kind_of_event not in TRADE_EVENTS | ACTIVITY_TRADE_EVENTS
            or e.get("status") not in ("EXECUTED", None)
            or e.get("deleted")
            or e.get("hidden")
            or e["id"] in seen
        ):
            continue
        seen.add(e["id"])
        remembered_trade = known.get(trade_id(salt, e["id"]))
        if isinstance(remembered_trade, dict) and remembered_trade.get("isin"):
            trades.setdefault(remembered_trade["isin"], []).append({k: v for k, v in remembered_trade.items() if k != "isin"})
            continue
        detail = details.get(e["id"])
        if not isinstance(detail, dict):
            raise BankSyncError("Trade Republic sent no detail for a trade; nothing was imported")
        isin = _isin(detail, e)
        if not isin:
            raise BankSyncError("Trade Republic sent a trade without its security; nothing was imported")
        rows = _rows(detail)
        units = rows.get("Aktien") or rows.get("Anteile") or rows.get("Shares")
        price = rows.get("Aktienkurs") or rows.get("Kurs") or rows.get("Price")
        if kind_of_event == TRANSFER_IN:
            price = ""  # priced by price_transfers from the position's average buy-in
        if units is None or price is None:
            raise BankSyncError(f"Trade Republic sent a {_text(kind_of_event, 60)} without units or price; nothing was imported")
        quantity = _detail_number(units, "number of units")
        buy_only = kind_of_event in BUY_ONLY_EVENTS
        raw_amount = (e.get("amount") or {}).get("value")
        # Activity log entries (transfers, free shares) carry no amount.
        amount = Decimal(0) if raw_amount is None and buy_only else _decimal(raw_amount, "amount")
        if quantity <= 0 or (amount == 0 and not buy_only):
            raise BankSyncError(f"Trade Republic sent a {_text(kind_of_event, 60)} without units or amount; nothing was imported")
        trades.setdefault(isin, []).append(
            {
                "external_id": trade_id(salt, e["id"]),
                "kind": "buy" if buy_only or amount < 0 else "sell",
                "date": _day(e.get("timestamp")).isoformat(),
                "quantity": quantity,
                "price": None
                if kind_of_event == TRANSFER_IN
                else format(_detail_number(price, "price").quantize(MICRO, rounding=ROUND_HALF_UP).normalize(), "f"),
                "fee": _money(abs(_detail_number(rows.get("Gebühr", "0"), "fee"))),
            }
        )
    for rows in trades.values():
        _round_units(rows)
    return trades


def _round_units(rows: list[dict]) -> None:
    """Units to Securo's six decimals, so that the position after each trade is the exact one rounded.

    Rounding each trade alone lets the errors add up past Securo's tolerance
    over many small trades; rounding the running position keeps the sum
    within half a millionth of the exact one. Rows read from a remembered
    sync already carry a string and stay as they are.
    """
    exact = previous = Decimal(0)
    for t in sorted(rows, key=lambda t: (t["date"], t["kind"] != "buy", t["external_id"])):
        step = Decimal(t["quantity"]) if isinstance(t["quantity"], str) else t["quantity"]
        exact += step if t["kind"] == "buy" else -step
        position = exact.quantize(MICRO, rounding=ROUND_HALF_UP)
        quantity = abs(position - previous) or MICRO
        previous = position if abs(position - previous) else previous + (quantity if t["kind"] == "buy" else -quantity)
        t["quantity"] = format(quantity.normalize(), "f")


def price_transfers(trades: dict[str, list[dict]], positions: dict[str, tuple[Decimal, Decimal]]) -> None:
    """Price each transfer in from Trade Republic's average buy-in (`positions[isin]`: units held, average).

    A transfer carries no price; Trade Republic keeps the cost it was told in
    the position's average buy-in. What that average says the position cost,
    less what its buys cost, is what the transferred units cost. Without a
    held position, or with a sale in between (the average then no longer
    adds up this way), the average itself is used, and 0 without either.
    """
    for isin, rows in trades.items():
        transfers = [t for t in rows if t["price"] is None]
        if not transfers:
            continue
        units, average = positions.get(isin, (Decimal(0), Decimal(0)))
        price = average
        moved = sum((Decimal(t["quantity"]) for t in transfers), Decimal(0))
        if units > 0 and moved > 0 and not any(t["kind"] == "sell" for t in rows):
            bought = sum((Decimal(t["quantity"]) * Decimal(t["price"]) for t in rows if t["price"] is not None), Decimal(0))
            implied = (units * average - bought) / moved
            if implied > 0:
                price = implied
        for t in transfers:
            t["price"] = format(price.quantize(MICRO, rounding=ROUND_HALF_UP).normalize(), "f")


TRADE_KEYS = {"external_id", "kind", "date", "quantity", "price", "fee", "isin"}


def valid_known(known: dict) -> dict[str, dict]:
    """The remembered trades that are well formed; any other is dropped and read again."""
    valid = {}
    for key, t in (known or {}).items():
        try:
            ok = (
                isinstance(t, dict)
                and set(t) == TRADE_KEYS
                and t["external_id"] == key
                and t["kind"] in ("buy", "sell")
                and isinstance(t["isin"], str)
                and len(t["isin"]) == 12
                and Decimal(t["quantity"]) > 0
                and Decimal(t["price"]) >= 0
                and Decimal(t["fee"]) >= 0
                and dt.date.fromisoformat(t["date"]) is not None
            )
        except InvalidOperation, TypeError, ValueError, KeyError:
            ok = False
        if ok:
            valid[key] = t
    return valid


def closed_holding_rows(trades: dict[str, list[dict]], events: list[dict], salt: str, held: set[str]) -> list[dict]:
    """A position no longer in the depot, reported closed with its trades so Securo keeps its history."""
    names = {trade_id(salt, e.get("id")): _text(e.get("title"), 255) for e in events if isinstance(e, dict)}
    rows = []
    for isin, isin_trades in sorted(trades.items()):
        if isin in held:
            continue
        left = sum((Decimal(t["quantity"]) * (1 if t["kind"] == "buy" else -1) for t in isin_trades), Decimal(0))
        if abs(left) > Decimal("0.0001"):
            # Sold out but its trades don't come to zero (a split Trade
            # Republic doesn't list): no history rather than a wrong one.
            continue
        name = next((names[t["external_id"]] for t in isin_trades if names.get(t["external_id"])), isin)
        rows.append(
            {
                "external_id": f"tr-pos-{isin}",
                "name": name,
                "currency": "EUR",
                "quantity": "0",
                "current_value": "0.00",
                "is_withdrawn": True,
                "isin": isin,
                "trades": isin_trades,
            }
        )
    return rows


def check_trades_add_up(holdings: list[dict], trades: dict[str, list[dict]]) -> None:
    """Each position's buys minus sells must give its units, as Securo checks them.

    Securo drops a set that doesn't add up with only a log line; failing here
    names the position instead, so an event this plugin does not map yet
    (a transfer, a bonus share) is seen.
    """
    for holding in holdings:
        rows = trades.get(holding["isin"], [])
        target = Decimal(holding["quantity"])
        position = sum((Decimal(t["quantity"]) * (1 if t["kind"] == "buy" else -1) for t in rows), Decimal(0))
        largest = max([abs(target), *(Decimal(t["quantity"]) for t in rows)])
        if abs(position - target) > max(Decimal("0.0001"), largest * Decimal("0.000001")):
            raise BankSyncError(
                f"the trades of {holding['isin']} do not add up to its units in Trade Republic "
                "(an event type this plugin does not map yet?); nothing was imported"
            )


def holding_rows(portfolio: dict, prices: dict[str, Decimal], types: dict[str, str]) -> list[dict]:
    """One holding per position, valued at `prices[isin]`; `types[isin]` is the instrument's typeId."""
    categories = portfolio.get("categories") if isinstance(portfolio, dict) else None
    if not isinstance(categories, list):
        raise BankSyncError("Trade Republic sent an unreadable depot; holdings were not changed")
    rows = []
    for category in categories:
        positions = category.get("positions") if isinstance(category, dict) else None
        if not isinstance(positions, list):
            raise BankSyncError("Trade Republic sent an unreadable depot category; holdings were not changed")
        for p in positions:
            isin = str(p.get("isin") or "")
            if isin not in prices:
                raise BankSyncError("Trade Republic sent no price for a position; holdings were not changed")
            size = _decimal(p.get("netSize"), "position size")
            buy_in = _decimal(p.get("averageBuyIn") or 0, "buy-in price")
            rows.append(
                {
                    "external_id": f"tr-pos-{isin}",
                    "name": _text(p.get("name"), 255) or isin,
                    "type": HOLDING_TYPES.get(types.get(isin, ""), "investment"),
                    "currency": "EUR",
                    "quantity": format(size.normalize(), "f"),
                    "unit_price": str(prices[isin]),
                    "current_value": _money(size * prices[isin]),
                    "purchase_price": _money(size * buy_in),
                    "isin": isin,
                }
            )
    return rows
