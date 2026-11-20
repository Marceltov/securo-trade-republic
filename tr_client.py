"""A minimal, read-only Trade Republic client.

The login and websocket protocol follow pytr (https://github.com/pytr-org/pytr,
MIT), reduced to what a read-only sync needs. There is deliberately no code here
that places orders, changes savings plans or moves money: `ALLOWED_SUBSCRIPTIONS`
is the complete list of what this client can ask for.
"""
from __future__ import annotations

import base64
import hashlib
import json
import re
import time

HOST = 'https://api.traderepublic.com'
WS_URL = 'wss://api.traderepublic.com'
USER_AGENT = ('Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) '
              'Chrome/146.0.0.0 Safari/537.36')
# Build version and platform of the web frontend, sent on the v2 login calls.
APP_VERSION = '2.2631.13'
WEB_PLATFORM = 'web-pro'
COOKIE_NAMES = frozenset({'tr_session', 'tr_refresh', 'tr_device', 'tr_claims', 'tr_external_id', 'JSESSIONID'})
POLL_SECONDS = 2
HTTP_TIMEOUT = 15

LOGIN_ERRORS = {
    'PROCESS_GONE': 'the login request expired',
    'ALREADY_PROCESSED': 'the login request was rejected or already used',
    'NOT_FOUND': 'Trade Republic does not know this login request',
    'TOO_MANY_REQUESTS': 'too many login attempts; wait before trying again',
    'AUTHENTICATION_ERROR': 'phone number or PIN not accepted',
}


class NeedsAuth(Exception):
    """A person has to act (confirm the login in the app); Securo pauses the connection."""


class AuthError(Exception):
    """The credentials were refused."""


class BankSyncError(Exception):
    """Trade Republic answered with something this plugin does not understand."""


def normalize_phone(raw: str) -> str:
    """Any common German notation of a phone number as +49…; other countries keep their prefix."""
    digits = re.sub(r'[\s()./-]', '', str(raw or ''))
    if digits.startswith('00'):
        digits = '+' + digits[2:]
    elif digits.startswith('0'):
        digits = '+49' + digits[1:]
    if not re.fullmatch(r'\+[1-9][0-9]{6,14}', digits):
        raise AuthError('the phone number is not valid; enter it as +49… or 0…')
    return digits


def _jwt_exp(token: str | None) -> float | None:
    try:
        payload = token.split('.')[1]
        return float(json.loads(base64.urlsafe_b64decode(payload + '=' * (-len(payload) % 4)))['exp'])
    except (AttributeError, IndexError, KeyError, TypeError, ValueError):
        return None


class TrSession:
    """A Trade Republic web session: login with app confirmation, renewal, account data."""

    def __init__(self, phone: str, pin: str, cookies: dict | None, *, http=None, sleep=time.sleep, clock=time.monotonic):
        if http is None:
            import requests
            http = requests.Session()
        self.phone = phone
        self.pin = pin
        self._http = http
        self._sleep = sleep
        self._clock = clock
        self._cookies = {k: str(v) for k, v in (cookies or {}).items() if k in COOKIE_NAMES}
        self.device_id = hashlib.sha512(f'securo-trade-republic|{phone}'.encode()).hexdigest()

    @property
    def cookies(self) -> dict[str, str]:
        return dict(self._cookies)

    def _request(self, method: str, path: str, *, headers=None, **kw):
        response = self._http.request(
            method, HOST + path, headers={'User-Agent': USER_AGENT, **(headers or {})},
            cookies=dict(self._cookies), timeout=HTTP_TIMEOUT, **kw)
        for name, value in dict(response.cookies).items():
            if name in COOKIE_NAMES:
                self._cookies[name] = value
        return response

    def _login_headers(self) -> dict[str, str]:
        device = {
            'stableDeviceId': self.device_id,
            'browser': 'Chrome',
            'browserVersion': '146.0.0.0',
            'os': 'Linux',
            'osVersion': '',
            'timezone': 'Europe/Berlin',
            'timezoneOffset': -60,
            'screen': '1920x1080x24',
            'preferredLanguages': ['de'],
            'numberOfCores': 4,
        }
        return {
            'X-TR-Device-Info': base64.b64encode(json.dumps(device).encode()).decode(),
            'X-TR-App-Version': APP_VERSION,
            'X-Tr-Platform': WEB_PLATFORM,
            'Accept-Language': 'de',
        }

    @staticmethod
    def _login_error(response) -> str:
        try:
            code = response.json()['errors'][0]['errorCode']
        except (ValueError, KeyError, IndexError, TypeError):
            return f'Trade Republic refused the login (HTTP {response.status_code})'
        return 'Trade Republic refused the login: ' + LOGIN_ERRORS.get(code, str(code)[:40])

    def login(self, wait_seconds: int = 50) -> None:
        """Start a login and wait for its confirmation in the Trade Republic app."""
        self._cookies.clear()
        headers = self._login_headers()
        response = self._request('POST', '/api/v2/auth/web/login',
                                 json={'phoneNumber': self.phone, 'pin': self.pin}, headers=headers)
        if response.status_code in (401, 403) or (400 <= response.status_code < 500 and response.status_code != 429):
            raise AuthError(self._login_error(response))
        if not response.ok:
            raise BankSyncError(self._login_error(response))
        process_id = (response.json() or {}).get('processId')
        if not process_id or not re.fullmatch(r'[A-Za-z0-9-]{1,64}', str(process_id)):
            raise BankSyncError('Trade Republic started no login confirmation')
        deadline = self._clock() + wait_seconds
        while True:
            process = self._request('GET', f'/api/v2/auth/web/login/processes/{process_id}', headers=headers)
            if not process.ok:
                raise AuthError(self._login_error(process))
            body = process.json() or {}
            if body.get('requiredAction') == 'AUTHENTICATOR_VERIFICATION':
                raise NeedsAuth('this account confirms logins with an authenticator code; '
                                'authenticator codes are not supported, only the confirmation in the Trade Republic app')
            status = body.get('status')
            if status in ('CONFIRMED', 'COMPLETED'):
                return
            if status != 'PENDING':
                raise AuthError('the login was not confirmed in the Trade Republic app')
            if self._clock() >= deadline:
                raise NeedsAuth('confirm the login in the Trade Republic app, then press Sync again')
            self._sleep(POLL_SECONDS)

    def renew(self) -> bool:
        """Renew the short-lived session from the stored login; False when a new login is needed."""
        exp = _jwt_exp(self._cookies.get('tr_refresh'))
        if exp is None or exp <= time.time() + 30:
            return False
        response = self._request('GET', '/api/v1/auth/web/session')
        if response.status_code in (401, 403):
            return False
        if not response.ok:
            raise BankSyncError(f'Trade Republic did not renew the session (HTTP {response.status_code})')
        return True

    def securities_account_number(self) -> str:
        response = self._request('GET', '/api/v2/auth/account')
        if not response.ok:
            raise BankSyncError(f'Trade Republic did not return the account settings (HTTP {response.status_code})')
        number = (response.json() or {}).get('securitiesAccountNumber')
        if not number:
            raise BankSyncError('Trade Republic returned no securities account')
        return str(number)
