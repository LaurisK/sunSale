#!/usr/bin/env bash
#
# Install the pre-push gate into .git/hooks/.
#
# Deliberately NOT `git config core.hooksPath .githooks`, which is what this
# repo used to do. `core.hooksPath` resolves against the *working tree*, so a
# tracked hooks directory vanishes the moment you check out a branch that does
# not carry it -- and git runs no hook and prints no warning. That is exactly
# how a release snapshot on master (which never tracked .githooks/) was pushed
# past a gate that looked installed: release v0.6.1, red on ruff and mypy.
#
# .git/hooks/ is per-clone and outside the working tree, so the gate survives
# every checkout, rebase and branch switch. It is not version-controlled, which
# is why this installer is -- run it once per clone.
#
# Usage:
#   scripts/install-hooks.sh

set -euo pipefail

root="$(git rev-parse --show-toplevel)"
hook="$root/.git/hooks/pre-push"

# The old mechanism silently wins over .git/hooks/ if it is left set.
if [[ "$(git config --get core.hooksPath || true)" == ".githooks" ]]; then
    git config --unset core.hooksPath
    echo "Unset core.hooksPath (it pointed at the branch-dependent .githooks/)."
fi

mkdir -p "$root/.git/hooks"
cat > "$hook" <<'HOOK'
#!/usr/bin/env bash
#
# Block a push whose working tree would fail CI.
#
# Installed by scripts/install-hooks.sh -- edit the installer, not this copy.
#
# pre-push rather than pre-commit deliberately: commits stay fast and can be
# freely made mid-work, while the gate lands at the moment that actually costs
# something -- a red CI run on master.
#
# Bypass for a genuine emergency with `git push --no-verify`.

set -uo pipefail

root="$(git rev-parse --show-toplevel)"

# Fail closed. A tree with no checks.sh is a tree whose gates cannot be run,
# which is not the same thing as a tree that passes them.
if [[ ! -x "$root/scripts/checks.sh" ]]; then
    echo "pre-push: scripts/checks.sh is missing or not executable." >&2
    echo "          Refusing to push a tree whose gates cannot be run." >&2
    echo "          Bypass with --no-verify if this is deliberate." >&2
    exit 1
fi

if ! "$root/scripts/checks.sh"; then
    echo
    echo "pre-push: refusing to push -- the checks above fail, and so would CI."
    echo "          Re-run with scripts/checks.sh, or bypass with --no-verify."
    exit 1
fi
HOOK

chmod +x "$hook"
echo "Installed $hook"
