# Contributing to sunSale

Thanks for your interest in improving sunSale. Bug reports, feature ideas, and pull
requests are all welcome.

## Before you start

- **Bugs & features:** open an issue at
  <https://github.com/LaurisK/sunSale/issues> first for anything non-trivial, so we can
  agree on the approach before you write code.
- **Read** [`CONVENTIONS.md`](CONVENTIONS.md) — the coding, docstring, testing, and
  integration-check conventions the codebase follows. Architecture and the control-loop
  contract live in [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) and
  [`docs/MODULES.md`](docs/MODULES.md).

## Development setup

```bash
python -m venv venv && source venv/bin/activate
pip install -r requirements_dev.txt

scripts/install-hooks.sh                  # gate pushes on the checks below
```

`contract/`, `pipeline/`, and the pure helpers run without Home Assistant imports, so most
of the suite runs under plain `pytest`.

## Running the checks

```bash
scripts/checks.sh            # ruff + mypy + pytest -- exactly what CI runs
scripts/checks.sh --fast     # ruff + mypy only, skipping the suite
```

Run the script rather than the individual tools. It mirrors
[`.github/workflows/ci.yml`](.github/workflows/ci.yml) argument for argument,
including the **paths**: CI lints `custom_components tools tests`, so a local
`ruff check custom_components/sun_sale/` can pass while CI fails on `tools/` or
`tests/`. A local command that differs from CI is worse than no local command —
it hands out confidence it has not earned.

`ruff` and `mypy` are pinned exactly in `requirements_dev.txt` and in the
workflow. Both add rules in minor releases, so floating them means an upstream
release can turn master red with no change here. Bump the pins deliberately,
with any resulting fixes in the same commit.

`scripts/install-hooks.sh` (above) writes a `pre-push` hook into `.git/hooks/`
that runs the same script and refuses a push that would fail CI. `git push
--no-verify` bypasses it for a genuine emergency.

It installs into `.git/hooks/` rather than setting `core.hooksPath` to a tracked
directory on purpose. `core.hooksPath` resolves against the **working tree**, so
a tracked hooks directory disappears as soon as you check out a branch that does
not carry it, and git runs no hook and says nothing — the gate looks installed
and is not. `.git/hooks/` is per-clone and outside the working tree, so it
survives every checkout. Run the installer once per clone; if `scripts/checks.sh`
is missing from the tree being pushed, the hook refuses rather than passing.

## Pull request checklist

- Keep PRs focused; one logical change per PR.
- Follow [`CONVENTIONS.md`](CONVENTIONS.md): Google-style docstrings on all functions, the
  comment rules, and the UI/secrets conventions.
- Add or update tests for the behaviour you change; `scripts/checks.sh` must pass.
- Every pipeline module that consumes or produces data needs a matching deep-check in
  `tools/checks/` (see *Integration-check coverage* in [`CONVENTIONS.md`](CONVENTIONS.md)).

## License of contributions

sunSale is licensed under the **GNU AGPL-3.0-or-later** (see [`LICENSE`](LICENSE)). By
contributing, you agree that your contributions are licensed under the same terms.

## Developer Certificate of Origin (DCO)

We use the [Developer Certificate of Origin](https://developercertificate.org/) instead of a
CLA. It is a lightweight statement that you wrote the contribution, or otherwise have the
right to submit it under the project's license.

**Every commit must be signed off.** Add a `Signed-off-by` line to your commit message:

```
Signed-off-by: Your Name <your.email@example.com>
```

Git adds this automatically when you commit with the `-s` flag:

```bash
git commit -s -m "Fix grid-export sign in derived observer"
```

The name and email must be real and match your Git author identity. Forgot to sign off your
last commit? Fix it with:

```bash
git commit --amend -s --no-edit
```

For a branch with several unsigned commits, sign them all off at once:

```bash
git rebase --signoff main
```

### What you certify

By signing off, you certify the Developer Certificate of Origin 1.1, reproduced below:

```
Developer Certificate of Origin
Version 1.1

Copyright (C) 2004, 2006 The Linux Foundation and its contributors.

Everyone is permitted to copy and distribute verbatim copies of this
license document, but changing it is not allowed.

Developer's Certificate of Origin 1.1

By making a contribution to this project, I certify that:

(a) The contribution was created in whole or in part by me and I
    have the right to submit it under the open source license
    indicated in the file; or

(b) The contribution is based upon previous work that, to the best
    of my knowledge, is covered under an appropriate open source
    license and I have the right under that license to submit that
    work with modifications, whether created in whole or in part
    by me, under the same open source license (unless I am
    permitted to submit under a different license), as indicated
    in the file; or

(c) The contribution was provided directly to me by some other
    person who certified (a), (b) or (c) and I have not modified
    it.

(d) I understand and agree that this project and the contribution
    are public and that a record of the contribution (including all
    personal information I submit with it, including my sign-off) is
    maintained indefinitely and may be redistributed consistent with
    this project or the open source license(s) involved.
```
