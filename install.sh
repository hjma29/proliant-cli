#!/bin/sh
# proliant-cli installer for Linux and macOS
# Usage: curl -fsSL https://raw.githubusercontent.com/hjma29/proliant-cli/main/install.sh | sh

set -e

# Silent install ping — counts installs by OS (no personal data sent)
curl -sf "https://proliant-cli.hjma29.workers.dev/install/unix" >/dev/null 2>&1 &

REPO="hjma29/proliant-cli"
BIN_NAME="proliant"
INSTALL_DIR="/usr/local/bin"

echo ""
echo "proliant-cli installer"
echo "══════════════════════════════════════"

# Detect OS and architecture
OS=$(uname -s)
ARCH=$(uname -m)

case "$OS" in
  Linux)
    case "$ARCH" in
      x86_64)  ASSET="proliant-cli-linux-x86" ;;
      aarch64) ASSET="proliant-cli-linux-arm64" ;;
      *)       echo "Unsupported architecture: $ARCH"; exit 1 ;;
    esac
    ;;
  Darwin)
    ASSET="proliant-cli-macos"
    ;;
  *)
    echo "Unsupported OS: $OS"
    echo "For Windows, run in PowerShell:"
    echo "  irm https://raw.githubusercontent.com/hjma29/proliant-cli/main/install.ps1 | iex"
    exit 1
    ;;
esac

# Resolve latest release URL via GitHub API
echo "Fetching latest release..."
API_URL="https://api.github.com/repos/${REPO}/releases/latest"
if command -v curl > /dev/null 2>&1; then
  RELEASE=$(curl -fsSL "$API_URL")
else
  echo "curl is required but not installed."; exit 1
fi

VERSION=$(echo "$RELEASE" | grep '"tag_name"' | sed 's/.*"tag_name": *"\([^"]*\)".*/\1/')
DOWNLOAD_URL="https://github.com/${REPO}/releases/download/${VERSION}/${ASSET}"

echo "Downloading $VERSION ($ASSET)..."
TMP=$(mktemp)
curl -fL --progress-bar "$DOWNLOAD_URL" -o "$TMP"
chmod +x "$TMP"

# Install — use sudo to write to /usr/local/bin, fall back to ~/.local/bin if sudo unavailable
if command -v sudo > /dev/null 2>&1; then
  sudo install -m 755 "$TMP" "$INSTALL_DIR/$BIN_NAME"
  rm -f "$TMP"
  echo ""
  echo "  Installed : $INSTALL_DIR/$BIN_NAME  (system-wide)"
  echo "  PATH      : already in PATH (no changes needed)"
else
  INSTALL_DIR="$HOME/.local/bin"
  mkdir -p "$INSTALL_DIR"
  mv "$TMP" "$INSTALL_DIR/$BIN_NAME"
  echo ""
  echo "  Installed : $INSTALL_DIR/$BIN_NAME  (user-only, no sudo)"

  # Check if ~/.local/bin is already in PATH
  case ":$PATH:" in
    *":$INSTALL_DIR:"*)
      echo "  PATH      : already contains $INSTALL_DIR"
      ;;
    *)
      # Auto-add to shell rc file
      SHELL_NAME=$(basename "$SHELL")
      if [ "$SHELL_NAME" = "zsh" ]; then
        RC_FILE="$HOME/.zshrc"
      else
        RC_FILE="$HOME/.bashrc"
      fi
      if ! grep -q "\.local/bin" "$RC_FILE" 2>/dev/null; then
        printf '\n# proliant: add ~/.local/bin to PATH\nexport PATH="$HOME/.local/bin:$PATH"\n' >> "$RC_FILE"
        echo "  PATH      : added \$HOME/.local/bin to $RC_FILE"
        echo "  Note      : run 'source $RC_FILE' or open a new terminal"
      else
        echo "  PATH      : $RC_FILE already references .local/bin (no changes made)"
      fi
      ;;
  esac
fi

echo "  Version:   $VERSION"
echo ""

# ── Tab completion setup ────────────────────────────────────────────────────
echo "Setting up tab completion..."

SHELL_NAME=$(basename "${SHELL:-sh}")

if [ "$SHELL_NAME" = "zsh" ]; then
  COMPLETIONS_DIR="$HOME/.zsh/completions"
  mkdir -p "$COMPLETIONS_DIR"

  # Write argcomplete-style dynamic zsh hook for proliant
  cat > "$COMPLETIONS_DIR/_proliant" << 'EOF'
#compdef proliant
__python_argcomplete_run() {
    if [[ -z "${ARGCOMPLETE_USE_TEMPFILES-}" ]]; then
        __python_argcomplete_run_inner "$@"; return
    fi
    local tmpfile="$(mktemp)"
    _ARGCOMPLETE_STDOUT_FILENAME="$tmpfile" __python_argcomplete_run_inner "$@"
    local code=$?; cat "$tmpfile"; rm "$tmpfile"; return $code
}
__python_argcomplete_run_inner() {
    if [[ -z "${_ARC_DEBUG-}" ]]; then
        "$@" 8>&1 9>&2 1>/dev/null 2>&1 </dev/null
    else
        "$@" 8>&1 9>&2 1>&9 2>&1 </dev/null
    fi
}
_python_argcomplete_proliant() {
    local IFS=$'\013'
    local completions
    completions=($(IFS="$IFS" \
        COMP_LINE="$BUFFER" \
        COMP_POINT="$CURSOR" \
        _ARGCOMPLETE=1 \
        _ARGCOMPLETE_SHELL="zsh" \
        _ARGCOMPLETE_SUPPRESS_SPACE=1 \
        __python_argcomplete_run proliant))
    local nosort=()
    local nospace=()
    if is-at-least 5.8; then nosort=(-o nosort); fi
    if [[ "${completions-}" =~ ([^\\]): && "${match[1]}" =~ [=/:] ]]; then
        nospace=(-S '')
    fi
    _describe "proliant" completions "${nosort[@]}" "${nospace[@]}"
}
autoload is-at-least
if [[ $zsh_eval_context == *func ]]; then
    _python_argcomplete_proliant "$@"
else
    compdef _python_argcomplete_proliant proliant
fi
EOF

  RC_FILE="$HOME/.zshrc"
  FPATH_LINE="fpath=($COMPLETIONS_DIR \$fpath)"

  if grep -qF "$COMPLETIONS_DIR" "$RC_FILE" 2>/dev/null; then
    echo "✓ Tab completion updated: $COMPLETIONS_DIR/_proliant"
  else
    # Insert before oh-my-zsh source line if present, else append
    if grep -q 'oh-my-zsh.sh' "$RC_FILE" 2>/dev/null; then
      sed -i.bak "/oh-my-zsh\.sh/i\\
# proliant tab completion\\
$FPATH_LINE
" "$RC_FILE"
    else
      printf '\n# proliant tab completion\n%s\nautoload -Uz compinit && compinit\n' "$FPATH_LINE" >> "$RC_FILE"
    fi
    echo "✓ Tab completion installed: $COMPLETIONS_DIR/_proliant"
    echo "  Run: source $RC_FILE"
  fi

else
  # bash
  RC_FILE="$HOME/.bashrc"
  if grep -q '_proliant_completion' "$RC_FILE" 2>/dev/null; then
    echo "✓ Tab completion already enabled in $RC_FILE"
  else
    cat >> "$RC_FILE" << 'EOF'

# proliant tab completion
__python_argcomplete_run() {
    if [[ -z "${ARGCOMPLETE_USE_TEMPFILES-}" ]]; then
        __python_argcomplete_run_inner "$@"; return
    fi
    local tmpfile="$(mktemp)"
    _ARGCOMPLETE_STDOUT_FILENAME="$tmpfile" __python_argcomplete_run_inner "$@"
    local code=$?; cat "$tmpfile"; rm "$tmpfile"; return $code
}
__python_argcomplete_run_inner() {
    if [[ -z "${_ARC_DEBUG-}" ]]; then
        "$@" 8>&1 9>&2 1>/dev/null 2>&1 </dev/null
    else
        "$@" 8>&1 9>&2 1>&9 2>&1 </dev/null
    fi
}
_proliant_completion() {
    local IFS=$'\013'
    local SUPPRESS_SPACE=0
    if compopt +o nospace 2>/dev/null; then SUPPRESS_SPACE=1; fi
    COMPREPLY=($(IFS="$IFS" \
        COMP_LINE="$COMP_LINE" \
        COMP_POINT="$COMP_POINT" \
        COMP_TYPE="$COMP_TYPE" \
        _ARGCOMPLETE_COMP_WORDBREAKS="$COMP_WORDBREAKS" \
        _ARGCOMPLETE=1 \
        _ARGCOMPLETE_SHELL="bash" \
        _ARGCOMPLETE_SUPPRESS_SPACE=$SUPPRESS_SPACE \
        __python_argcomplete_run proliant))
    if [[ $? != 0 ]]; then
        unset COMPREPLY
    elif [[ $SUPPRESS_SPACE == 1 ]] && [[ "${COMPREPLY-}" =~ [=/:]$ ]]; then
        compopt -o nospace
    fi
}
complete -o nospace -o default -o bashdefault -F _proliant_completion proliant
EOF
    echo "✓ Tab completion added to $RC_FILE"
    echo "  Run: source $RC_FILE"
  fi
fi

echo ""
echo "Run 'proliant --version' to verify."
echo ""
