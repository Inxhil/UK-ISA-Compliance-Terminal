# Security Policy

## Supported Versions

This is a single-maintainer project under active development towards v1.0.
Only the latest release receives fixes.

| Version | Supported          |
| ------- | ------------------ |
| 0.2.x   | :white_check_mark: |
| < 0.2   | :x:                |

## Reporting a Vulnerability

Please **do not open a public issue** for security problems.

Report privately through GitHub: go to the **Security** tab of this repository
and click **Report a vulnerability**. Include:

- what the issue is and where it occurs (file / function if known)
- steps to reproduce it
- the potential impact (e.g. could it expose API keys or modify user files?)

**What to expect**

- Acknowledgement within **7 days**
- An initial assessment within **14 days**, telling you whether the report is
  accepted or declined, and why
- If accepted, a fix in the next release, with credit to you in the release
  notes unless you would prefer to stay anonymous

This is a personal project maintained alongside university study, so response
times are best-effort.

## Scope

In scope:
- Handling of API keys (`.env` storage, logging, input prompts)
- Edits the app makes to local files (`config.yaml`, handbook, broker CSV imports)
- Parsing of data from external providers and imported broker CSVs
- Opening article links from the News pane

Out of scope:
- Vulnerabilities in third-party providers (Twelve Data, Finnhub, Yahoo Finance,
  Bank of England) or in dependencies themselves; please report those upstream
- The accuracy of market data, signals or calculations. This tool is
  quantitative modelling only and **not FCA-regulated financial advice**

## Security Design

- **Runs locally.** Nothing leaves your machine except ticker symbols and the
  search text you type.
- **API keys are optional** and stored only in `.env` (git-ignored, file mode 600).
  Keys are entered via hidden prompts, sent in HTTP headers only, and never logged.
- **File edits are safe by default:** changes made by the app are validated,
  comment-preserving, and keep a `.bak` backup.
- **Links:** only `http(s)` URLs are ever opened from the News pane.

## Advice for Users

- Never commit your `.env` file. If a key is exposed, revoke it in the
  provider's dashboard and generate a new one.
- Use free-tier keys. The app never needs keys with billing or trading permissions.
