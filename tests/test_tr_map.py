import sys
import unittest
from datetime import date
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import tr_client as c
import tr_map as m

SALT = '00' * 32


def event(id_, status='EXECUTED', value=-12.5, ts='2026-11-19T10:15:00.000+0000', **kw):
    e = {'id': id_, 'timestamp': ts, 'title': 'Example Shop', 'subtitle': 'Kartentransaktion',
         'amount': {'currency': 'EUR', 'value': value, 'fractionDigits': 2},
         'status': status, 'eventType': 'CARD_TRANSACTION', 'hidden': False, 'deleted': False}
    e.update(kw)
    return e


class Transactions(unittest.TestCase):
    def test_executed_and_pending_mapped(self):
        rows = m.transaction_rows([event('a'), event('b', status='PENDING', value=30, title='Jane Doe')])
        self.assertEqual(rows[0], {
            'external_id': 'tr-a', 'description': 'Example Shop', 'payee': 'Example Shop',
            'amount': '12.50', 'type': 'debit', 'date': '2026-11-19', 'currency': 'EUR', 'status': 'posted',
            'provider_category': 'CARD_TRANSACTION', 'raw_data': {'eventType': 'CARD_TRANSACTION', 'subtitle': 'Kartentransaktion'}})
        self.assertEqual((rows[1]['status'], rows[1]['type'], rows[1]['amount']), ('pending', 'credit', '30.00'))

    def test_date_is_berlin_day(self):
        rows = m.transaction_rows([event('a', ts='2026-11-19T23:30:00.000+0000')])
        self.assertEqual(rows[0]['date'], '2026-11-20')

    def test_cancelled_deleted_hidden_skipped(self):
        rows = m.transaction_rows([event('a', status='CANCELED'), event('b', deleted=True), event('c', hidden=True), event('d')])
        self.assertEqual([r['external_id'] for r in rows], ['tr-d'])

    def test_unknown_status_fails(self):
        with self.assertRaises(c.BankSyncError):
            m.transaction_rows([event('a', status='SOMETHING_NEW')])

    def test_missing_amount_fails(self):
        with self.assertRaises(c.BankSyncError):
            m.transaction_rows([event('a', amount=None)])

    def test_duplicate_event_ids_deduplicated(self):
        rows = m.transaction_rows([event('a'), event('a', value=-99)])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['amount'], '12.50')


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
        events, calls = self.run_paged([
            {'items': [event('a')], 'cursors': {'after': 'c1'}},
            {'items': [event('b')], 'cursors': {}},
        ])
        self.assertEqual([e['id'] for e in events], ['a', 'b'])
        self.assertEqual(calls, [None, 'c1'])

    def test_paging_stops_on_repeated_cursor(self):
        events, calls = self.run_paged([
            {'items': [event('a')], 'cursors': {'after': 'c1'}},
            {'items': [event('b')], 'cursors': {'after': 'c1'}},
            {'items': [event('x')], 'cursors': {}},
        ])
        self.assertEqual(calls, [None, 'c1'])

    def test_paging_stops_at_since(self):
        events, calls = self.run_paged([
            {'items': [event('a', ts='2026-11-19T10:00:00.000+0000'), event('b', ts='2026-11-01T10:00:00.000+0000')],
             'cursors': {'after': 'c1'}},
            {'items': [event('x', ts='2026-10-01T10:00:00.000+0000')], 'cursors': {}},
        ], since=date(2026, 11, 10))
        self.assertEqual([e['id'] for e in events], ['a'])
        self.assertEqual(calls, [None])

    def test_page_without_items_fails(self):
        with self.assertRaises(c.BankSyncError):
            self.run_paged([{'cursors': {}}])


class AccountRow(unittest.TestCase):
    def test_account_id_hides_account_number(self):
        row = m.account_row([{'accountNumber': 'DE00123456789012345678', 'currencyId': 'EUR', 'amount': 1234.5}], SALT)
        self.assertNotIn('123456789', row['external_id'])
        self.assertEqual((row['type'], row['balance'], row['currency'], row['masked_number']), ('checking', '1234.50', 'EUR', '5678'))
        self.assertEqual(row['institution_name'], 'Trade Republic')

    def test_no_cash_account_fails(self):
        with self.assertRaises(c.BankSyncError):
            m.account_row([], SALT)


def position(isin, size='2.5', itype='stock'):
    return {'isin': isin, 'netSize': size, 'averageBuyIn': '10.0', 'instrumentType': itype, 'name': 'Example ' + isin}


class Holdings(unittest.TestCase):
    PRICES = {'US0000000001': Decimal('100'), 'IE0000000002': Decimal('50.5'), 'XF0000000003': Decimal('80000')}
    TYPES = {'US0000000001': 'stock', 'IE0000000002': 'fund', 'XF0000000003': 'crypto'}

    def test_holdings_from_categories(self):
        portfolio = {'categories': [
            {'categoryType': 'stocksAndETFs', 'positions': [position('US0000000001'), position('IE0000000002', '4')]},
            {'categoryType': 'cryptos', 'positions': [position('XF0000000003', '0.01', 'crypto')]}]}
        rows = m.holding_rows(portfolio, self.PRICES, self.TYPES)
        self.assertEqual([(r['isin'], r['type'], r['quantity'], r['current_value']) for r in rows], [
            ('US0000000001', 'stock', '2.5', '250.00'),
            ('IE0000000002', 'etf', '4', '202.00'),
            ('XF0000000003', 'crypto', '0.01', '800.00')])
        self.assertEqual(rows[0]['external_id'], 'tr-pos-US0000000001')
        self.assertEqual(rows[0]['purchase_price'], '25.00')

    def test_missing_price_fails(self):
        portfolio = {'categories': [{'categoryType': 'stocksAndETFs', 'positions': [position('US0000000001')]}]}
        with self.assertRaises(c.BankSyncError):
            m.holding_rows(portfolio, {}, self.TYPES)

    def test_empty_depot_reported_empty_only_when_categories_present(self):
        self.assertEqual(m.holding_rows({'categories': [{'categoryType': 'stocksAndETFs', 'positions': []}]}, {}, {}), [])
        with self.assertRaises(c.BankSyncError):
            m.holding_rows({}, {}, {})


if __name__ == '__main__':
    unittest.main()
