#!/bin/sh
# =============================================================================
# standup — one-command setup. Works on macOS (Apple Silicon) and Linux.
#
#   ./install.sh [--tags neovim,fzf] [other ansible-playbook args]
#   curl -LsSf https://raw.githubusercontent.com/iwzbi/standup/main/install.sh | sh
#
# Idempotent: safe to re-run on an existing machine (upgrades/overwrites per
# the playbook's idempotency design — see memory/decisions.md).
# =============================================================================
set -eu

REPO_URL="https://github.com/iwzbi/standup.git"
STANDUP_DIR="${STANDUP_DIR:-$HOME/code/standup}"

log() { printf '\033[1;32m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m!!\033[0m %s\n' "$*"; }

OS="$(uname -s)"

# --- macOS: ensure Homebrew (install.sh owns this; the homebrew role only asserts) ---
if [ "$OS" = "Darwin" ]; then
  if ! command -v brew >/dev/null 2>&1; then
    log "Installing Homebrew (you may be asked for your password once)"
    NONINTERACTIVE=1 /bin/bash -c "$(curl -LsSf https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
  fi
  # Put brew on PATH for this process (ansible inherits it)
  if [ -x /opt/homebrew/bin/brew ]; then
    eval "$(/opt/homebrew/bin/brew shellenv)"
  fi
fi

# --- ensure git is available (fresh macOS may need Xcode Command Line Tools) ---
if ! command -v git >/dev/null 2>&1; then
  if [ "$OS" = "Darwin" ]; then
    warn "git not found. Run: xcode-select --install  (then re-run ./install.sh)"
    exit 1
  fi
  warn "git not found. Install it via your package manager and re-run."
  exit 1
fi

# --- locate or clone the repo ---
if [ -f ./configme.yaml ]; then
  REPO_DIR="$(pwd)"
elif [ -d "$STANDUP_DIR/.git" ]; then
  REPO_DIR="$STANDUP_DIR"
  log "Updating $REPO_DIR"
  git -C "$REPO_DIR" pull --ff-only
else
  log "Cloning $REPO_URL -> $STANDUP_DIR"
  mkdir -p "$(dirname "$STANDUP_DIR")"
  git clone "$REPO_URL" "$STANDUP_DIR"
  REPO_DIR="$STANDUP_DIR"
fi
cd "$REPO_DIR"

# --- ensure uv (Python package manager; installs ansible) ---
# Astral's install script is the primary path. In a restricted network ("red
# zone") where astral.sh is unreachable, fall back per OS: Homebrew on macOS
# (brew is already ensured above), pipx on Linux (dnf/apt installs pipx, which
# is also a system package in the `packages` role; needs PyPI reachable). Both
# land uv in ~/.local/bin, so PATH and the `tools` role's stat-gate stay
# consistent. See memory/decisions.md D011.
if ! command -v uv >/dev/null 2>&1; then
  log "Installing uv"
  if curl -LsSf --max-time 20 https://astral.sh/uv/install.sh -o /tmp/uv-install.sh; then
    sh /tmp/uv-install.sh
  else
    warn "astral.sh unreachable — falling back to OS package manager (D011)"
    case "$(uname -s)" in
      Darwin)
        brew install uv
        ;;
      Linux)
        if ! command -v pipx >/dev/null 2>&1; then
          if command -v dnf >/dev/null 2>&1; then
            sudo dnf install -y pipx
          elif command -v apt-get >/dev/null 2>&1; then
            sudo apt-get install -y pipx
          else
            warn "no supported package manager found (need dnf or apt-get) for pipx"
            exit 1
          fi
        fi
        pipx install uv
        ;;
      *)
        printf '!! Unsupported OS for uv fallback: %s\n' "$(uname -s)" >&2
        exit 1
        ;;
    esac
  fi
  export PATH="$HOME/.local/bin:$PATH"
fi

# --- sync the ansible environment from pyproject.toml + uv.lock ---
log "Syncing ansible environment (uv sync)"
uv sync

# --- run the playbook (args passed through, e.g. --tags neovim) ---
log "Running playbook: configme.yaml $*"
exec uv run ansible-playbook configme.yaml "$@"
