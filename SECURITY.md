# Security

## Reporting a vulnerability

Use **Report a vulnerability** on this repository's Security tab, never a public issue. It covers the plugin's code and the dependencies it ships in `requirements.txt`. You get an answer within seven days. The advisory is published with your name, unless you'd rather not be named.

Never post credentials, IBANs, account data or bank responses, also not in a private report: describe the shape instead.

## Supported versions

Only the newest release gets fixes; there are no backports.

## What each release carries

- `securo-trade-republic-X.Y.Z.tar.gz`: the source archive, with a build provenance attestation (`gh attestation verify securo-trade-republic-X.Y.Z.tar.gz --repo marceltov/securo-trade-republic`).
- `securo-trade-republic-vX.Y.Z.cdx.json`: a CycloneDX SBOM of the runtime dependencies (`requirements.txt`).

## How this repository watches

- Private vulnerability reporting and secret scanning with push protection (also over the tests).
- CodeQL through `.github/workflows/codeql.yml`, on every pull request, every push to `main` and weekly. It leaves out the tests (`.github/codeql/codeql-config.yml`): their fixtures hold invented personal data on purpose.
- Dependabot alerts for every dependency; Dependabot pull requests for the dev tools and GitHub Actions.
- A daily `pip-audit` of `requirements.txt` (`.github/workflows/audit.yml`).
