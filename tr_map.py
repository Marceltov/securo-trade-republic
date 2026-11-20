"""Trade Republic answers as Securo contract 1 records.

Anything this module does not recognise stops the sync with BankSyncError: an
unexpected answer must never become an empty list, which Securo would read as
"everything is gone" and archive.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import hmac
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from zoneinfo import ZoneInfo

from tr_client import BankSyncError

BERLIN = ZoneInfo('Europe/Berlin')
CENT = Decimal('0.01')
# Timeline statuses: what each becomes in Securo. None: not reported.
STATUSES = {'EXECUTED': 'posted', 'PENDING': 'pending', 'CANCELED': None, 'CANCELLED': None}
HOLDING_TYPES = {'stock': 'stock', 'fund': 'etf', 'crypto': 'crypto', 'bond': 'investment', 'derivative': 'investment'}
MAX_PAGES = 1000


def _digest(salt: str, prefix: str, *values) -> str:
    content = '\x1f'.join(str(v) for v in values).encode()
    return prefix + hmac.new(bytes.fromhex(salt), content, hashlib.sha256).hexdigest()[:40]


def _decimal(value, what: str) -> Decimal:
    try:
        number = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        raise BankSyncError(f'Trade Republic sent an unreadable {what}; nothing was imported') from None
    if not number.is_finite():
        raise BankSyncError(f'Trade Republic sent an invalid {what}; nothing was imported')
    return number


def _money(value: Decimal) -> str:
    return str(value.quantize(CENT, rounding=ROUND_HALF_UP))


def _day(timestamp) -> dt.date:
    try:
        moment = dt.datetime.strptime(str(timestamp), '%Y-%m-%dT%H:%M:%S.%f%z')
    except ValueError:
        try:
            moment = dt.datetime.fromisoformat(str(timestamp))
        except ValueError:
            raise BankSyncError('Trade Republic sent an event without a readable date; nothing was imported') from None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=dt.timezone.utc)
    return moment.astimezone(BERLIN).date()


def _text(value, size: int) -> str:
    return ' '.join(str(value or '').split())[:size]


def account_row(cash: list, salt: str) -> dict:
    if not isinstance(cash, list) or len(cash) != 1 or not isinstance(cash[0], dict):
        raise BankSyncError('Trade Republic returned no single cash account; nothing was imported')
    entry = cash[0]
    number = str(entry.get('accountNumber') or '')
    currency = str(entry.get('currencyId') or '')
    if not number or len(currency) != 3:
        raise BankSyncError('Trade Republic returned an incomplete cash account; nothing was imported')
    return {
        'external_id': _digest(salt, 'tr-acct-', number),
        'name': 'Trade Republic',
        'type': 'checking',
        'balance': _money(_decimal(entry.get('amount'), 'balance')),
        'currency': currency.upper(),
        'masked_number': number[-4:],
        'institution_name': 'Trade Republic',
    }


def transaction_rows(events: list[dict]) -> list[dict]:
    rows, seen = [], set()
    for e in events:
        if not isinstance(e, dict) or not e.get('id'):
            raise BankSyncError('Trade Republic sent a timeline event without an id; nothing was imported')
        if e['id'] in seen:
            continue
        seen.add(e['id'])
        status = e.get('status')
        if status not in STATUSES:
            raise BankSyncError(f'Trade Republic sent an unknown event status ({_text(status, 30)}); nothing was imported')
        if STATUSES[status] is None or e.get('deleted') or e.get('hidden'):
            continue
        amount = e.get('amount')
        if not isinstance(amount, dict) or amount.get('value') is None:
            raise BankSyncError('Trade Republic sent an event without an amount; nothing was imported')
        value = _decimal(amount['value'], 'amount')
        title = _text(e.get('title'), 200) or 'Trade Republic'
        rows.append({
            'external_id': f"tr-{_text(e['id'], 120)}",
            'description': title,
            'payee': title,
            'amount': _money(abs(value)),
            'type': 'debit' if value < 0 else 'credit',
            'date': _day(e.get('timestamp')).isoformat(),
            'currency': str(amount.get('currency') or 'EUR').upper(),
            'status': STATUSES[status],
            'provider_category': _text(e.get('eventType'), 100) or None,
            'raw_data': {'eventType': e.get('eventType'), 'subtitle': _text(e.get('subtitle'), 200)},
        })
    return rows


async def paged_events(read_page, since: dt.date | None) -> list[dict]:
    """All timeline events (newest first) down to `since`, following the `after` cursor."""
    events, cursor, used = [], None, set()
    for _ in range(MAX_PAGES):
        page = await read_page(cursor)
        items = page.get('items') if isinstance(page, dict) else None
        if not isinstance(items, list):
            raise BankSyncError('Trade Republic sent an unreadable timeline page; nothing was imported')
        for item in items:
            if since is not None and _day(item.get('timestamp')) < since:
                return events
            events.append(item)
        cursor = (page.get('cursors') or {}).get('after')
        if not cursor or cursor in used:
            return events
        used.add(cursor)
    raise BankSyncError('the Trade Republic timeline did not end; nothing was imported')


def holding_rows(portfolio: dict, prices: dict[str, Decimal], types: dict[str, str]) -> list[dict]:
    """One holding per position, valued at `prices[isin]`; `types[isin]` is the instrument's typeId."""
    categories = portfolio.get('categories') if isinstance(portfolio, dict) else None
    if not isinstance(categories, list):
        raise BankSyncError('Trade Republic sent an unreadable depot; holdings were not changed')
    rows = []
    for category in categories:
        positions = category.get('positions') if isinstance(category, dict) else None
        if not isinstance(positions, list):
            raise BankSyncError('Trade Republic sent an unreadable depot category; holdings were not changed')
        for p in positions:
            isin = str(p.get('isin') or '')
            if isin not in prices:
                raise BankSyncError('Trade Republic sent no price for a position; holdings were not changed')
            size = _decimal(p.get('netSize'), 'position size')
            buy_in = _decimal(p.get('averageBuyIn') or 0, 'buy-in price')
            rows.append({
                'external_id': f'tr-pos-{isin}',
                'name': _text(p.get('name'), 255) or isin,
                'type': HOLDING_TYPES.get(types.get(isin, ''), 'investment'),
                'currency': 'EUR',
                'quantity': format(size.normalize(), 'f'),
                'unit_price': str(prices[isin]),
                'current_value': _money(size * prices[isin]),
                'purchase_price': _money(size * buy_in),
                'isin': isin,
            })
    return rows
