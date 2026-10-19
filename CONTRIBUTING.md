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
test tiers — see [ARCHITECTURE.md](ARCHITECTURE.md); agent-oriented working
rules live in [AGENTS.md](AGENTS.md). The project uses `uv` with a virtualenv
at `.venv` (Python 3.14):

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

### CI parity

tox resolves the newest allowed Home Assistant and test-harness versions,
while `uv.lock` pins the pair the local venv uses; the newer harness
escalates deprecations to failures, so a green local run can still fail CI.
`.tox/py` is the local CI-parity environment — run the suite there before
pushing a change that touches Home Assistant APIs.

Wire facts are not guessed from behaviour: the record lives in this repo,
at `docs/api-contract.md`, where every claim carries a provenance tag
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

### Writing tests

- A transport double's `run()` must park until `close()` — the real
  socket's contract. One that returns immediately makes the engine's
  supervision restart in a tight cycle, and `async_block_till_done()`
  never settles.
- `tests/fake_api.py` models the server: a setup write REPLACES the
  stored object with the posted body, and the write operation owns the
  merge — an operation that drops a key fails the round trip there. The
  fake does not model the write-settle lag.
- Consumption fixtures use hour-aligned timestamps: the recorder rejects
  a statistics row off the hour grid and aborts the whole import.
- Home Assistant's service targeting silently drops Unavailable
  entities, so a test of a write to an unavailable node calls the entity
  method directly.
- The suite is parametrized over °C/°F runs; `conftest.py` converts the
  setup temperature fields (away offset, max temperature limit) for the
  Fahrenheit pass.
- Time-dependent tests patch
  `custom_components.smartbox.mapping.dt_util.utcnow` and assert
  timezone-independently.
- The availability tests use real sleeps with tight windows; treat them
  as the first suspects if the suite ever flakes intermittently.

### Translations

- `strings.json` keeps `[%key:common::...]` references (Home Assistant
  resolves them at runtime); the files under `translations/` must inline
  them, and `tests/test_translations.py` keeps the set in step.
- The Home Assistant frontend compiles every translated string as an
  ICU message: braces are placeholders there, and a stray brace renders
  as `Translation error: MALFORMED_ARGUMENT`. The gate keeps
  `strings.json`, the translation files and `services.yaml`
  brace-safe.
- "Boost" and "True Radiant" stay untranslated in es/fr/de (brand
  terms).

### Shipped-text conventions

Shipped text — code comments, tests, docs — states the reason for a
decision, may reference repository history (versions, commits) where
that helps the reader, and cites the in-repo record by section. It never
points at agent instructions or local-only files;
`tests/test_docs_references.py` keeps the citations resolving and the
dead references out.

## Releasing and live deploys

- Soak/dev deploys: build the archive from the committed tree
  (`git archive <commit>:custom_components/smartbox`), replace
  `/config/custom_components/smartbox` on the Home Assistant box and
  restart Home Assistant. Never leave a backup copy of the integration
  under `custom_components/` — Home Assistant imports every subdirectory
  there.
- Releases: bump the version and update [CHANGELOG.md](CHANGELOG.md);
  HACS consumes the repository (`hacs.json`), and `main` carries the
  released line.

## License

By contributing, you agree that your contributions will be licensed under its MIT License.