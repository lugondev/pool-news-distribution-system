# Contributing

Thanks for your interest in improving this project! Contributions of all kinds
are welcome — bug reports, feature requests, docs, and code.

## Development setup

```bash
git clone <repo-url> && cd news-aggregator
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# Local config (copy from the bundled examples)
cp .env.example .env
cp config/settings.yaml.example     config/settings.yaml
cp config/sources.yaml.example      config/sources.yaml
cp config/social_agents.yaml.example config/social_agents.yaml
cp config/sim_personas.yaml.example  config/sim_personas.yaml

# Redis must be running locally (or set REDIS_URL)
python main.py   # dashboard at http://localhost:8000
```

AI credentials are configured in the dashboard (Settings → AI providers), not in
`.env`.

## Running tests

```bash
pytest
```

## Guidelines

- **Never commit secrets.** API keys, tokens, and real `config/*.yaml` files are
  gitignored — keep them that way. Only `*.example` files belong in git.
- Match the existing code style (naming, structure, comment density).
- Keep pull requests focused; one logical change per PR.
- Update `README.md` / `CLAUDE.md` when behavior or configuration changes.
- Add or update tests for new functionality where practical.

## Reporting bugs

Open an issue with steps to reproduce, expected vs. actual behavior, and relevant
logs (with any secrets redacted).
