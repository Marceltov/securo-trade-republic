# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

securo-trade-republic: a read-only Securo plugin that syncs Trade Republic (cash account, depot positions, each position's trades) through Trade Republic's web login.

## Commands
- `uv sync`, then `uv run pytest -q` (one test: `uv run pytest -q tests/test_tr_map.py -k name`).
- `uv run ruff format`, `uv run ruff check --fix`; CI runs `ruff check`, `ruff format --check` and pytest.
- After any change to `uv.lock`: `uv export --frozen --no-dev --no-emit-project --no-header --format requirements-txt > requirements.txt` (a test fails otherwise). Securo's runner installs that file and refuses anything but hash-pinned index packages.
- Release: bump `version` in `pyproject.toml` and `securo-plugin.json`, then `git tag -a vX.Y.Z -m "<headline>" && git push origin vX.Y.Z`.

## Architecture
- `tr_bank.py`: `plugin.sync`, Securo's entrypoint (`tr_bank:plugin`); credentials, state and the order of reads.
- `tr_client.py`: the only module that talks to Trade Republic: the web login and the websocket. `ALLOWED_SUBSCRIPTIONS` is the whole list of what it may read.
- `tr_map.py`: Trade Republic's answers as Securo contract 1 records (accounts, transactions, holdings, trades): pure functions.
- Modules are flat with a `tr_` prefix: Securo's runner puts the repository root first on the import path, so a plain `client.py` would shadow a dependency's module of that name.

## Rules
- Follows Marceltov/engineering-conventions; the rules here win where they differ. No docs site: the README is the documentation (Securo's catalog shows it).
- Read-only: no subscription for orders, savings plans or payouts may be added to `ALLOWED_SUBSCRIPTIONS`.
- No phone number, PIN, session cookie, account number, name or amount in git, a test fixture, an error message or a log. Fixtures are invented values in Trade Republic's shapes.
- An answer that can't be read fails the sync; it never returns an empty list in place of data it couldn't read.
- Dependabot auto-merges patch and minor bumps of dev tools and GitHub Actions once CI passes: the one case where a merge happens without the user saying so. Runtime dependencies change by hand.
- Code, errors and docs in English. The manifest's user-facing texts are in English and German: Securo shows a plugin's text in the user's language (falling back to English), and this plugin's users bank in Germany. Other languages only when someone asks for one.
- Architectural decisions go to `docs/adr/NNNN-*.md` (MADR).
