# Security Policy

## Reporting a vulnerability

If you discover a security vulnerability, please report it privately rather than
opening a public issue.

- Email: **lugondev@gmail.com**
- Include: a description of the issue, steps to reproduce, affected version/commit,
  and any potential impact.

Please give us a reasonable amount of time to investigate and address the issue
before any public disclosure. We will acknowledge your report as soon as possible.

## Scope & handling secrets

- This project stores credentials (AI API keys, Telegram tokens, S3 keys, database
  URLs) in `config/settings.yaml` and `.env`. Both are **gitignored** — never commit
  real values. Only `*.example` files belong in version control.
- If you find a leaked secret in the repository or its history, report it privately
  using the contact above so it can be rotated.
