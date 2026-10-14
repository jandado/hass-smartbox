# Contribution guidelines

Contributing to this project should be as easy and transparent as possible, whether it's:

- Reporting a bug
- Discussing the current state of the code
- Submitting a fix
- Proposing new features

## Github is used for everything

Github is used to host code, to track issues and feature requests, as well as accept pull requests.

Pull requests are the best way to propose changes to the codebase.

1. Fork the repo and create your branch from `main`.
2. If you've changed something, update the documentation.
3. Make sure your code lints and type-checks (ruff + mypy, see [Local
   development](#local-development)).
4. Test you contribution.
5. Issue that pull request!

## Any contributions you make will be under the MIT Software License

In short, when you submit code changes, your submissions are understood to be under the same [MIT License](http://choosealicense.com/licenses/mit/) that covers the project. Feel free to contact the maintainers if that's a concern.

## Report bugs using Github's [issues](../../issues)

GitHub issues are used to track public bugs.
Report a bug by [opening a new issue](../../issues/new/choose); it's that easy!

## Write bug reports with detail, background, and sample code

**Great Bug Reports** tend to have:

- A quick summary and/or background
- Steps to reproduce
  - Be specific!
  - Give sample code if you can.
- What you expected would happen
- What actually happens
- Notes (possibly including why you think this might be happening, or stuff you tried that didn't work)

People *love* thorough bug reports. I'm not even kidding.

## Use a Consistent Coding Style

Use ruff to make sure the code follows the style.

## Local development

For how the code is structured — the layer rules, the repository map and the
test tiers — see [ARCHITECTURE.md](ARCHITECTURE.md). The project uses `uv`
with a virtualenv at `.venv` (Python 3.14):

```
uv sync --all-extras --dev
.venv/bin/python -m pytest -q
.venv/bin/ruff check custom_components tests tools
.venv/bin/mypy custom_components tools
```

The test suite runs across all cores by default (`-n auto`, from
`addopts`); pass `-n0` when running a single test, where worker
startup costs more than the test itself.

CI runs the same ruff and mypy checks plus the test suite through tox.

Wire facts are not guessed from behaviour, and they are not recorded in
another repository either: the record lives in this repo, at
`docs/api-contract.md`, where every claim carries a provenance tag
(`[live YYYY-MM-DD]`, `[code file:line]`, `[app]`, `[unverified]`,
`[superseded: ...]`). Three documents, three roles: the contract settles
wire ACCEPTANCE (live-probed facts, open questions, the deliberate
divergences from the retired library), `docs/webapi-spec.md` holds the
decoded web app's statics, and `docs/api-ws-user.md` holds the CURRENT
vendor apps' behavior plus the parity checklist (re-run it per vendor app
release). Where an app-derived document disagrees with a live
observation, the live observation wins and the disagreement is recorded,
not silently resolved. Code comments cite the in-repo file (for example
`docs/api-contract.md §5.1`) instead of restating a rule as folklore, and
`tools/live_probe.py` is the CLI for verifying a claim against real
hardware.

The three documents are maintained in this repository; new probe results
and corrections are applied in place, and a test keeps every citation
pointing at the in-repo copies, so a clone or archive of this repo is
self-contained.

## License

By contributing, you agree that your contributions will be licensed under its MIT License.