import asyncio
import json
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import tr_bank as b
import tr_client as c

PHONE, PIN = '+491701234567', '9876'
SALT = 'ab' * 32
COOKIES = {'tr_refresh': 'REFRESHTOKENVALUE', 'tr_session': 'SESSIONTOKENVALUE'}

READS = {
    'cash': [{'accountNumber': 'DE00123456789012345678', 'currencyId': 'EUR', 'amount': 100.0}],
    'compactPortfolioByType': {'categories': [{'categoryType': 'stocksAndETFs', 'positions': [
        {'isin': 'US0000000001', 'netSize': '2', 'averageBuyIn': '10', 'instrumentType': 'stock', 'name': 'Example'}]}]},
    'instrument': {'exchangeIds': ['LSX'], 'typeId': 'stock'},
    'ticker': {'last': {'price': '12.5'}},
    'timelineTransactions': {'items': [{'id': 'e1', 'timestamp': '2026-11-19T10:00:00.000+0000', 'title': 'Example Shop',
                                        'amount': {'currency': 'EUR', 'value': -5}, 'status': 'EXECUTED',
                                        'eventType': 'CARD_TRANSACTION'}], 'cursors': {}},
}


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
        self._cookies = {'tr_refresh': 'NEWREFRESH', 'tr_session': 'NEWSESSION'}

    def securities_account_number(self):
        return '0123'

    async def read(self, kind, payload=None, timeout=20):
        self.reads.append((kind, payload))
        if self.read_error:
            raise self.read_error
        return READS[kind]

    async def close(self):
        pass


def context(secrets=None):
    base = {'phone': '0170 1234567', 'pin': PIN, 'id_salt': SALT, 'session': json.dumps(COOKIES)}
    return SimpleNamespace(secrets=base if secrets is None else secrets, config={}, securo={})


def sync(params=None, ctx=None, **session_kw):
    FakeSession.instances.clear()
    factory = lambda phone, pin, cookies: FakeSession(phone, pin, cookies, **session_kw)
    return asyncio.run(b.Plugin(session_factory=factory).sync(params or {}, ctx or context()))


class Sync(unittest.TestCase):
    def test_reuses_session_no_login(self):
        result = sync()
        s = FakeSession.instances[0]
        self.assertFalse(s.logged_in)
        self.assertEqual(s.phone, PHONE)
        self.assertEqual(json.loads(result['secrets']['session']), COOKIES)

    def test_expired_session_logs_in_and_returns_new_cookies(self):
        result = sync(renews=False)
        self.assertTrue(FakeSession.instances[0].logged_in)
        self.assertEqual(json.loads(result['secrets']['session'])['tr_refresh'], 'NEWREFRESH')

    def test_renew_rejected_falls_back_to_login(self):
        sync(ctx=context({'phone': PHONE, 'pin': PIN, 'id_salt': SALT}))
        self.assertTrue(FakeSession.instances[0].logged_in)

    def test_login_timeout_is_needs_auth(self):
        with self.assertRaises(c.NeedsAuth):
            sync(renews=False, login_error=c.NeedsAuth('confirm the login in the Trade Republic app, then press Sync again'))

    def test_missing_credentials_needs_auth(self):
        with self.assertRaises(b.NeedsAuth):
            sync(ctx=context({}))

    def test_ws_error_fails_without_data(self):
        with self.assertRaises(c.BankSyncError):
            sync(read_error=c.BankSyncError('Trade Republic refused cash'))

    def test_unexpected_exception_becomes_bank_sync_error(self):
        with self.assertRaises(b.BankSyncError) as ctx:
            sync(renews=ConnectionError(f'boom {PHONE} {PIN} REFRESHTOKENVALUE'))
        self.assertIn('ConnectionError', str(ctx.exception))

    def test_error_messages_never_contain_secrets(self):
        for kw in ({'renews': ConnectionError(f'x {PHONE} {PIN} REFRESHTOKENVALUE')},
                   {'read_error': RuntimeError(f'y {PIN} SESSIONTOKENVALUE DE00123456789012345678')}):
            with self.assertRaises(Exception) as ctx:
                sync(**kw)
            for secret in (PHONE, PIN, 'REFRESHTOKENVALUE', 'SESSIONTOKENVALUE', '123456789012345678'):
                self.assertNotIn(secret, str(ctx.exception))

    def test_payload_shape_contract_1(self):
        result = sync()
        data = result['data']
        self.assertEqual(data['contract'], 1)
        self.assertEqual(len(data['accounts']), 1)
        self.assertEqual(data['accounts'][0]['transactions'][0]['external_id'], 'tr-e1')
        self.assertEqual(data['holdings'][0]['current_value'], '25.00')
        self.assertNotIn('account_external_id', data['holdings'][0])
        self.assertEqual(result['secrets']['id_salt'], SALT)

    def test_ticker_uses_first_exchange(self):
        sync()
        reads = FakeSession.instances[0].reads
        self.assertIn(('ticker', {'id': 'US0000000001.LSX'}), reads)
        self.assertIn(('compactPortfolioByType', {'secAccNo': '0123'}), reads)

    def test_since_passed_to_timeline(self):
        sync({'since': '2026-11-25'})
        # the only event (2026-11-19) is older than since
        # -> no transactions, but the account is still reported
        result = sync({'since': '2026-11-25'})
        self.assertEqual(result['data']['accounts'][0]['transactions'], [])

    def test_new_salt_created_once(self):
        result = sync(ctx=context({'phone': PHONE, 'pin': PIN, 'session': json.dumps(COOKIES)}))
        self.assertEqual(len(result['secrets']['id_salt']), 64)


if __name__ == '__main__':
    unittest.main()
