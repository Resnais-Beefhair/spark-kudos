# Contributing

Please keep KUDOS small, honest and read-only. Discuss broad new integrations before
adding dependencies. A new adapter must degrade visibly when its source is missing,
and its browser payload must be an explicit allowlist.

## Local development

```bash
uv sync --extra dev
uv run ruff check .
uv run pytest
node --check src/sisu_kudos/web/app.js
node scripts/test_i18n.cjs
uv build
uv run --with playwright playwright install chromium
uv run --with playwright python scripts/qa_browser.py
```

Python 3.12 is the compatibility floor. Tests must use temporary directories and
synthetic fixtures. They must not require the maintainer's machines, SSH access,
agent databases, cloud credentials or a particular GPU. Do not add a network call to
a unit test. Browser QA must own and clean up its disposable browser and server.

## Changes

1. Reproduce a bug with a focused failing test where practical.
2. Make the smallest durable change. Preserve independently bounded collectors.
3. Add English and Finnish catalog entries together, including titles, dialogs and
   accessible labels. Never translate user data through arbitrary DOM rewriting.
4. Update the configuration reference/examples when the schema changes.
5. Bump `pyproject.toml`, `src/sisu_kudos/__init__.py` and asset versions together for
   a material release, then update the changelog.
6. Run the checks above, the privacy gate, and the relevant real browser test.

## Release gate

- Build the wheel and source distribution from a clean tree.
- Install the wheel in a clean virtual environment, with no optional repositories.
- Check the CLI, a healthy one-host API, optional two-host behavior, remote failure,
  English/Finnish persistence, mobile overflow and browser errors.
- Run `python scripts/check_public.py`. Review the actual diff and complete publish
  set, including screenshots, for private information. A scanner is not a substitute
  for human review.
- Verify documentation links and examples. Record real evidence; do not invent a CI
  result when hosted CI has not run.
- Publish/tag only after the repository owner approves the target, visibility and
  license. This source tree does not create a remote or publish itself.

Avoid diagnostic dumps in public issues. Use the minimum redacted fixture that
reproduces the behavior. See [SECURITY.md](SECURITY.md).
