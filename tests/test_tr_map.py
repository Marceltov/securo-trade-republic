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


ISIN = 'XX0000000001'
NB = '\xa0'


def trade_event(id_, event_type='TRADING_SAVINGSPLAN_EXECUTED', value=-50.0, status='EXECUTED',
                ts='2020-01-02T09:00:00.000+0000'):
    return {'id': id_, 'timestamp': ts, 'title': 'Example ETF', 'subtitle': 'Sparplan ausgeführt',
            'amount': {'currency': 'EUR', 'value': value, 'fractionDigits': 2},
            'status': status, 'eventType': event_type, 'hidden': False, 'deleted': False}


def header(isin=ISIN):
    return {'title': 'Du hast 50,00 € gespart', 'type': 'header',
            'action': {'type': 'instrumentDetail', 'payload': isin}, 'data': {'status': 'executed'}}


def row(title, text):
    return {'title': title, 'style': 'plain', 'detail': {'type': 'text', 'text': text}}


def new_detail(units, price, fee='Kostenlos', isin=ISIN):
    """The layout of TRADING_* events: units and price on the info page behind the Transaktion row."""
    info = {'sections': [{'title': 'Transaktion', 'type': 'header', 'data': {'status': 'executed'}},
                         {'type': 'table', 'data': [row('Aktienkurs', f'{price}{NB}€'), row('Aktien', units),
                                                    row('Summe', f'50,00{NB}€')]}]}
    transaktion = {'title': 'Transaktion', 'style': 'plain',
                   'detail': {'type': 'text', 'text': f'{units} ×  {price}{NB}€',
                              'action': {'type': 'infoPage', 'payload': info}}}
    return {'id': 'd', 'sections': [header(isin), {'title': 'Übersicht', 'type': 'table', 'data': [
        row('Sparplan', 'Ausgeführt'), transaktion, row('Gebühr', fee), row('Summe', f'50,00{NB}€')]}]}


def old_detail(units, price, fee, isin=ISIN):
    """The layout of ORDER_EXECUTED: its own Transaktion table."""
    return {'id': 'd', 'sections': [header(isin),
                                    {'title': 'Übersicht', 'type': 'table', 'data': [row('Status', 'Ausgeführt'), row('Orderart', 'Kauf')]},
                                    {'title': 'Transaktion', 'type': 'table', 'data': [
                                        row('Anteile', units), row('Aktienkurs', f'{price}{NB}€'),
                                        row('Gebühr', f'{fee}{NB}€'), row('Gesamt', f'101,00{NB}€')]}]}


class Trades(unittest.TestCase):
    def test_order_becomes_a_buy_on_its_execution_day(self):
        trades = m.trade_rows([trade_event('o', 'ORDER_EXECUTED', -101.0)], {'o': old_detail('2', '50,00', '1,00')}, SALT)
        self.assertEqual(list(trades), [ISIN])
        t = trades[ISIN][0]
        self.assertEqual({k: t[k] for k in ('kind', 'date', 'quantity', 'price', 'fee')},
                         {'kind': 'buy', 'date': '2020-01-02', 'quantity': '2', 'price': '50', 'fee': '1.00'})

    def test_savings_plan_run_is_a_buy_without_fee(self):
        t = m.trade_rows([trade_event('s')], {'s': new_detail('0,5', '100,00')}, SALT)[ISIN][0]
        self.assertEqual((t['kind'], t['quantity'], t['price'], t['fee']), ('buy', '0.5', '100', '0.00'))

    def test_sell_is_a_sell_with_positive_units(self):
        t = m.trade_rows([trade_event('x', 'TRADING_TRADE_EXECUTED', 300.0)], {'x': new_detail('3', '100,00', f'1,00{NB}€')}, SALT)[ISIN][0]
        self.assertEqual((t['kind'], t['quantity'], t['fee']), ('sell', '3', '1.00'))

    def test_cancelled_order_is_not_a_trade(self):
        self.assertEqual(m.trade_rows([trade_event('c', 'ORDER_EXECUTED', status='CANCELED')], {}, SALT), {})

    def test_failed_savings_plan_run_is_not_a_trade(self):
        self.assertEqual(m.trade_rows([trade_event('f', 'TRADING_SAVINGSPLAN_EXECUTION_FAILED')], {}, SALT), {})

    def test_crypto_units_keep_six_decimals(self):
        t = m.trade_rows([trade_event('b')], {'b': new_detail('0,0012345678', '1.234,56')}, SALT)[ISIN][0]
        self.assertEqual((t['quantity'], t['price']), ('0.001235', '1234.56'))

    def test_unknown_unit_event_fails_naming_it(self):
        with self.assertRaises(c.BankSyncError) as caught:
            m.trade_rows([trade_event('t', 'SSP_SECURITIES_TRANSFER_INCOMING', 0.0)], {}, SALT)
        self.assertIn('SSP_SECURITIES_TRANSFER_INCOMING', str(caught.exception))

    def test_trade_detail_without_units_fails(self):
        detail = old_detail('2', '50,00', '1,00')
        detail['sections'][2]['data'] = [r for r in detail['sections'][2]['data'] if r['title'] != 'Anteile']
        with self.assertRaises(c.BankSyncError):
            m.trade_rows([trade_event('o', 'ORDER_EXECUTED')], {'o': detail}, SALT)

    def test_trade_detail_without_isin_fails(self):
        detail = new_detail('1', '10,00')
        detail['sections'][0]['action'] = None
        with self.assertRaises(c.BankSyncError):
            m.trade_rows([trade_event('s')], {'s': detail}, SALT)

    def test_external_id_is_stable_and_salted(self):
        def ids(salt):
            return m.trade_rows([trade_event('s')], {'s': new_detail('1', '10,00')}, salt)[ISIN][0]['external_id']
        self.assertEqual(ids(SALT), ids(SALT))
        self.assertNotEqual(ids(SALT), ids('11' * 32))
        self.assertNotIn('s', ids(SALT).removeprefix('tr-trade-'))

    def test_execution_day_is_berlin_local(self):
        t = m.trade_rows([trade_event('s', ts='2020-01-01T23:30:00.000+0000')], {'s': new_detail('1', '10,00')}, SALT)[ISIN][0]
        self.assertEqual(t['date'], '2020-01-02')

    def test_trades_of_two_positions_are_kept_apart(self):
        trades = m.trade_rows([trade_event('a'), trade_event('b')],
                              {'a': new_detail('1', '10,00'), 'b': new_detail('2', '20,00', isin='XX0000000002')}, SALT)
        self.assertEqual(sorted(trades), [ISIN, 'XX0000000002'])

    def test_a_detail_without_a_fee_row_has_no_fee(self):
        detail = new_detail('1', '10,00')
        detail['sections'][1]['data'] = [r for r in detail['sections'][1]['data'] if r['title'] != 'Gebühr']
        self.assertEqual(m.trade_rows([trade_event('s')], {'s': detail}, SALT)[ISIN][0]['fee'], '0.00')
