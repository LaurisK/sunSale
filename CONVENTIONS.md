# sunSale — coding conventions

Conventions every contribution is expected to follow. Architecture and the control-loop
contract are documented separately in [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) and
[`docs/MODULES.md`](docs/MODULES.md).

## Language & tooling

- **Python 3.12+.** Use modern syntax (`X | None`, `match`, built-in generics).
- **Formatting / linting:** `ruff` (line length 100; rule sets `E`, `F`, `I`, `UP`).
- **Typing:** annotate public functions; `mypy` must pass (`ignore_missing_imports = true`).
- Run `ruff check custom_components/sun_sale/` and `mypy custom_components/sun_sale/`
  before opening a PR.

## Docstrings

**Every function has a docstring** — including private, static, and helper functions. Use
**Google-style** docstrings:

```python
def example(arg1: int, arg2: str) -> bool:
    """Brief one-line description.

    Args:
        arg1: What this argument represents.
        arg2: What this argument represents.

    Returns:
        What the return value means.

    Raises:
        ValueError: When and why this is raised (omit section if no exceptions).
    """
```

Rules:

- The opening line is a brief **imperative** statement (`Return …`, `Build …`, `Compute …`).
- Include `Args:`, `Returns:`, and `Raises:` sections wherever they add value. **Omit empty
  sections.**
- For trivially obvious one-purpose functions (e.g. a simple property getter), a single-line
  docstring is fine when the type annotation already makes the intent self-evident.
- For DAG `_compute()` overrides, a one-liner suffices when the parent class docstring already
  states the node's purpose.

## Comments

- **Section comments** (e.g. `# --- Section name ---`) may mark significant logical groups
  within a module. Keep them sparse.
- **Inline comments explain *why*, never *what*.** Add one only when the logic is non-obvious,
  counter-intuitive, or works around a specific constraint.
- Do not write comments that merely restate the code.

## Testing

- Add or update tests for any behaviour you change; the **full `pytest` suite must pass.**
- Prefer pure, HA-free tests. `contract/`, `pipeline/`, and the pure helpers import no Home
  Assistant package; inbound translators accept a duck-typed `hass` and are testable without
  the `homeassistant` package — keep new logic on that side of the boundary where practical.

## Integration-check coverage

Every pipeline module that **consumes or produces data** must have a corresponding deep-check
in the `tools/checks/` package. The check must validate:

- all data the module **consumes** — cross-checked against its upstream source (raw HA entity
  state, `snap.inputs`, or an upstream `snap.pipeline` key);
- all data the module **exposes** — every field in the debug-API serialization, including
  aggregate totals (sums, counts) verified against per-slot values.

**Servicing modules** that only route or actuate without producing pipeline data (e.g. an
event router, the inverter writer) are exempt.

When adding a new pipeline module:

1. Expose its output in `orchestration/debug_view.py` under `pipeline` (or `outputs` for final
   deliverables).
2. Add a `check_<module>()` function + result dataclass and a `<Module>CheckWidget` in the
   matching `tools/checks/<domain>.py`, re-exported from `tools/checks/__init__.py`.
3. Wire the widget into `tools/checks/app.py` and call `check_<module>()` in
   `tools/checks/cli.py`.

## UI conventions

- **All user-facing time is 24-hour** (`hour12: false` / `%H:%M` / `HH:mm`). Never am/pm.
- Entity option strings stay `snake_case` for Home Assistant; human labels are mapped in the
  panel/UI layer, not baked into entity state.

## Secrets

Never commit credentials. Dev tools read `--url`/`--token`, the `HA_URL`/`HA_TOKEN` env vars,
or a local `tools/secrets.json` (gitignored — copy `tools/secrets.example.json`).
