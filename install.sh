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

# Install to ~/.local/bin (no sudo required)
INSTALL_DIR="$HOME/.local/bin"
mkdir -p "$INSTALL_DIR"
mv "$TMP" "$INSTALL_DIR/$BIN_NAME"
echo ""
echo "  Installed : $INSTALL_DIR/$BIN_NAME"

# Check if ~/.local/bin is already in PATH
case ":$PATH:" in
  *":$INSTALL_DIR:"*)
    echo "  PATH      : already contains $INSTALL_DIR"
    ;;
  *)
    SHELL_NAME=$(basename "$SHELL")
    if [ "$SHELL_NAME" = "zsh" ]; then
      RC_FILE="$HOME/.zshrc"
    else
      RC_FILE="$HOME/.bashrc"
    fi
    if ! grep -qE "^[^#]*\.local/bin" "$RC_FILE" 2>/dev/null; then
      printf '\n# proliant: add ~/.local/bin to PATH\nexport PATH="$HOME/.local/bin:$PATH"\n' >> "$RC_FILE"
      echo "  PATH      : added \$HOME/.local/bin to $RC_FILE"
      echo "  Note      : run 'source $RC_FILE' or open a new terminal"
    else
      echo "  PATH      : $RC_FILE already references .local/bin (no changes made)"
    fi
    ;;
esac

echo "  Version:   $VERSION"
echo ""

# ── Tab completion setup ────────────────────────────────────────────────────
echo "Setting up tab completion (dynamic)..."

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
    local raw
    raw=($(IFS="$IFS" \
        COMP_LINE="$BUFFER" \
        COMP_POINT="$CURSOR" \
        _ARGCOMPLETE=1 \
        _ARGCOMPLETE_SHELL="tcsh" \
        _ARGCOMPLETE_SUPPRESS_SPACE=1 \
        __python_argcomplete_run proliant))
    local -a quoted nospace
    local word trimmed suffix
    if [[ ${#raw[@]} -eq 1 ]]; then
        word="${raw[1]}"
        trimmed="$word"
        suffix=""
        if [[ "$word" == *" " ]]; then
            trimmed="${word% }"
            suffix=" "
        fi
        if [[ "$trimmed" =~ [=/:]$ ]]; then
            # Continuable value (directory path, --flag=): keep native
            # backslash-escaping so the shell can keep extending the token
            # without having to track an unmatched open quote, and tell
            # compadd not to append a trailing space.
            nospace=(-S '')
            quoted+=("${(q)trimmed}${suffix}")
        elif [[ "$trimmed" =~ ^[A-Za-z0-9._/:@%+=,-]*$ ]]; then
            quoted+=("${trimmed}${suffix}")
        else
            # Sole, fully-resolved match (e.g. "SY 480 Gen10 2"): safe to
            # wrap in single quotes since no further completion of this
            # token is expected.
            quoted+=("'${trimmed//\'/\'\\\'\'}'${suffix}")
        fi
    else
        # Ambiguous common-prefix candidates: fall back to native
        # backslash-escaping so the shared prefix zsh computes across
        # entries stays a single valid token (no unmatched open-quote state
        # to manage while the user keeps typing).
        for word in "${raw[@]}"; do
            if [[ "$word" == *" " ]]; then
                quoted+=("${(q)${word% }} ")
            else
                quoted+=("${(q)word}")
            fi
        done
    fi
    compadd -Q -U -V '-proliant' "${nospace[@]}" -a quoted
}
if [[ $zsh_eval_context == *func ]]; then
    _python_argcomplete_proliant "$@"
else
    compdef _python_argcomplete_proliant proliant
fi
EOF

  RC_FILE="$HOME/.zshrc"
  FPATH_LINE="fpath=($COMPLETIONS_DIR \$fpath)"

  if grep -qF "$COMPLETIONS_DIR" "$RC_FILE" 2>/dev/null; then
    echo "✓ Tab completion enabled (dynamic)"
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
    echo "✓ Tab completion enabled (dynamic)"
    echo "  Run: source $RC_FILE"
  fi

else
  # bash
  RC_FILE="$HOME/.bashrc"
  if [ -f "$RC_FILE" ] && grep -q '_proliant_completion' "$RC_FILE" 2>/dev/null; then
    # An older version of the completion block may already be installed.
    # Strip it out so the block below always reflects the current version
    # (re-running this script upgrades an existing installation in place).
    awk '
      $0 == "# proliant tab completion" { skip=1; next }
      skip && $0 == "complete -o nospace -o default -o bashdefault -F _proliant_completion proliant" { skip=0; next }
      !skip { print }
    ' "$RC_FILE" > "$RC_FILE.tmp" && mv "$RC_FILE.tmp" "$RC_FILE"
  fi
  if [ -f "$RC_FILE" ] && grep -q '_proliant_completion' "$RC_FILE" 2>/dev/null; then
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
    local raw
    raw=($(IFS="$IFS" \
        COMP_LINE="$COMP_LINE" \
        COMP_POINT="$COMP_POINT" \
        COMP_TYPE="$COMP_TYPE" \
        _ARGCOMPLETE_COMP_WORDBREAKS="$COMP_WORDBREAKS" \
        _ARGCOMPLETE=1 \
        _ARGCOMPLETE_SHELL="tcsh" \
        _ARGCOMPLETE_SUPPRESS_SPACE=$SUPPRESS_SPACE \
        __python_argcomplete_run proliant))
    if [[ $? != 0 ]]; then
        unset COMPREPLY
        return
    fi
    COMPREPLY=()
    local word trimmed suffix
    if [[ ${#raw[@]} -eq 1 ]]; then
        word="${raw[0]}"
        trimmed="$word"
        suffix=""
        if [[ "$word" == *" " ]]; then
            trimmed="${word% }"
            suffix=" "
        fi
        if [[ $SUPPRESS_SPACE == 1 ]] && [[ "$trimmed" =~ [=/:]$ ]]; then
            # Continuable value (directory path, --flag=): keep native
            # backslash-escaping so the shell can keep extending the token
            # without having to track an unmatched open quote, and let bash
            # know not to auto-append a trailing space.
            compopt -o nospace 2>/dev/null
            COMPREPLY+=("$(printf '%q' "$trimmed")${suffix}")
        elif [[ "$trimmed" =~ ^[A-Za-z0-9._/:@%+=,-]*$ ]]; then
            COMPREPLY+=("${trimmed}${suffix}")
        else
            # Sole, fully-resolved match (e.g. "SY 480 Gen10 2"): safe to
            # wrap in single quotes since no further completion of this
            # token is expected.
            COMPREPLY+=("'${trimmed//\'/\'\\\'\'}'${suffix}")
        fi
    else
        # Ambiguous common-prefix candidates: fall back to native
        # backslash-escaping so the shared prefix bash computes across
        # entries stays a single valid token (no unmatched open-quote state
        # to manage while the user keeps typing).
        for word in "${raw[@]}"; do
            trimmed="$word"
            suffix=""
            if [[ "$word" == *" " ]]; then
                trimmed="${word% }"
                suffix=" "
            fi
            COMPREPLY+=("$(printf '%q' "$trimmed")${suffix}")
        done
    fi
}
complete -o nospace -o default -o bashdefault -F _proliant_completion proliant
EOF
    echo "✓ Tab completion added to $RC_FILE"
    echo "  Run: source $RC_FILE"
  fi
fi

echo ""
echo "Run 'proliant version' to verify."
echo ""
