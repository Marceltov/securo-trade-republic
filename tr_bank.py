"""Read-only Trade Republic provider for Securo >= 0.20.0.

Plugin API: https://github.com/securo-finance/securo/blob/v0.20.0/docs/plugins/bank-provider-plugins.md
Syncs only when a person presses Sync (no schedule in the manifest): a login lasts
24 hours, and a new one needs a confirmation in the Trade Republic app.
"""
from __future__ import annotations

import datetime as dt
import json
import secrets as pysecrets
from decimal import Decimal

from tr_client import AuthError, BankSyncError, NeedsAuth, TrSession, normalize_phone
from tr_map import TRADE_EVENTS, _day as _event_day_of, remembered, trade_id, account_row, holding_rows, paged_events, trade_rows, transaction_rows

__all__ = ['AuthError', 'BankSyncError', 'NeedsAuth', 'Plugin', 'plugin']

CONTRACT = 1
LOGIN_WAIT_SECONDS = 50


def _credentials(context) -> tuple[str, str]:
    secrets = context.secrets or {}
    phone, pin = str(secrets.get('phone') or '').strip(), str(secrets.get('pin') or '').strip()
    if not phone or not pin:
        raise NeedsAuth('enter the phone number and PIN of your Trade Republic account')
    return normalize_phone(phone), pin


def _salt(context) -> str:
    salt = (context.secrets or {}).get('id_salt') or pysecrets.token_hex(32)
    try:
        if len(bytes.fromhex(salt)) != 32:
            raise ValueError
    except ValueError:
        raise BankSyncError('the saved ID salt is invalid; do not change it, or account IDs change') from None
    return salt


def _stored_cookies(context) -> dict:
    try:
        cookies = json.loads((context.secrets or {}).get('session') or '{}')
    except ValueError:
        return {}
    return cookies if isinstance(cookies, dict) else {}


def _since(params) -> dt.date | None:
    value = (params or {}).get('since')
    if not value:
        return None
    try:
        return dt.date.fromisoformat(str(value)[:10])
    except ValueError:
        raise BankSyncError('Securo sent an invalid sync window') from None


async def _prices(session, portfolio: dict) -> tuple[dict[str, Decimal], dict[str, str]]:
    prices, types = {}, {}
    for category in portfolio.get('categories') or []:
        for position in (category or {}).get('positions') or []:
            isin = str(position.get('isin') or '')
            if not isin or isin in prices:
                continue
            instrument = await session.read('instrument', {'id': isin})
            exchanges = instrument.get('exchangeIds') if isinstance(instrument, dict) else None
            if not exchanges:
                raise BankSyncError('Trade Republic lists no exchange for a position; holdings were not changed')
            types[isin] = str(instrument.get('typeId') or '')
            ticker = await session.read('ticker', {'id': f'{isin}.{exchanges[0]}'})
            price = ((ticker or {}).get('last') or {}).get('price') if isinstance(ticker, dict) else None
            if price is None:
                raise BankSyncError('Trade Republic sent no price for a position; holdings were not changed')
            prices[isin] = Decimal(str(price))
    return prices, types


def _known_trades(params) -> dict:
    """Trades read on an earlier sync (see tr_map.remembered); anything unexpected is ignored and read again."""
    state = (params or {}).get('state')
    known = state.get('trades') if isinstance(state, dict) else None
    return known if isinstance(known, dict) else {}


def _event_day(event: dict) -> dt.date:
    return _event_day_of(event.get('timestamp'))


class Plugin:
    def __init__(self, session_factory=None):
        self._factory = session_factory or (lambda phone, pin, cookies: TrSession(phone, pin, cookies))

    async def sync(self, params: dict, context) -> dict:
        phone, pin = _credentials(context)
        salt = _salt(context)
        since = _since(params)
        session = self._factory(phone, pin, _stored_cookies(context))
        try:
            if not session.renew():
                session.login(wait_seconds=LOGIN_WAIT_SECONDS)
            account = account_row(await session.read('cash'), salt)
            sec_acc_no = session.securities_account_number()
            portfolio = await session.read('compactPortfolioByType', {'secAccNo': sec_acc_no})
            prices, types = await _prices(session, portfolio)
            holdings = holding_rows(portfolio, prices, types)

            async def page(after):
                return await session.read('timelineTransactions', {'after': after} if after else None)
            # The whole timeline every time: Securo replaces a position's
            # plugin trades with the set reported, so a partial set would drop
            # history. Cash bookings still follow Securo's window.
            events = await paged_events(page, None)
            account['transactions'] = transaction_rows(
                [e for e in events if since is None or _event_day(e) >= since])
            known = _known_trades(params)
            details = {}
            for e in events:
                if (e.get('eventType') in TRADE_EVENTS and e.get('status') == 'EXECUTED'
                        and e['id'] not in details and trade_id(salt, e['id']) not in known):
                    details[e['id']] = await session.read('timelineDetailV2', {'id': e['id']})
            trades = trade_rows(events, details, salt, known)
            for holding in holdings:
                holding['trades'] = trades.get(holding['isin'], [])
        except (NeedsAuth, AuthError, BankSyncError):
            raise
        except Exception as exc:  # noqa: BLE001 - never pass a foreign message on: it may carry secrets
            raise BankSyncError(f'Trade Republic sync failed ({type(exc).__name__}); nothing was changed') from None
        finally:
            try:
                await session.close()
            except Exception:  # noqa: BLE001
                pass
        return {
            'secrets': {'id_salt': salt, 'session': json.dumps(session.cookies)},
            'state': {'trades': remembered(trades)},
            'data': {'contract': CONTRACT, 'accounts': [account], 'holdings': holdings},
        }


plugin = Plugin()
