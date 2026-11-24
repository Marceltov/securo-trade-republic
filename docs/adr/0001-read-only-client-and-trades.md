---
status: accepted
date: 2026-11-23
decision-makers: Marcel Bruckner
---

# A read-only client of our own, and each position's trades from the timeline

Recorded after the fact: the decisions below were taken between 2026-11-20 and 2026-11-22 and are in the history up to 0.2.1.

## Context and Problem Statement

Trade Republic has no official API and no FinTS. Securo should show the cash account, the depot and how each position grew, without a chore at every sync and without any code that could trade.

## Considered Options

- `pytr` as a dependency.
- An own client, following pytr's login and websocket protocol, reduced to what a read-only sync needs.

## Decision Outcome

Chosen option: **an own client**, because pytr ships order functions, a native browser-impersonation library and PyPI releases without provenance, and its paste-in login would be a daily chore.

- **Read-only by construction:** the websocket client may subscribe only to what `ALLOWED_SUBSCRIPTIONS` lists; there is no code for orders, savings plans or payouts.
- **No schedule:** a login needs a confirmation in the Trade Republic app and lasts 24 hours, so Securo syncs only when a person presses Sync.
- **Trades from the timeline:** every buy and sale (orders, savings plans, Saveback, round-ups) with its date, units, price and fee, read from each event's detail and remembered in the connection's state, so a later sync reads only new ones in detail. Units that came another way (free shares, transfers in) come from the activity log; a transfer carries no price and is priced from the position's average buy-in, which keeps the invested amount as the app shows it.
- **Fails closed:** an event that changes units in a way the plugin doesn't map, or a held position whose trades don't add up to its units, fails the sync with a message naming it; a sold-out position whose trades don't come to zero (a split Trade Republic doesn't list) comes in without history.

### Consequences

- Good, because each position's chart starts at its first buy, and every number comes from Trade Republic.
- Bad, because Trade Republic's web API can change without notice; an answer the plugin doesn't recognise fails the sync until the plugin follows.
- Bad, because the web session the plugin keeps could trade: it is a Securo plugin secret, and the server holding it must be treated accordingly.
- Bad, because a stock split Trade Republic doesn't list leaves that position without history.
