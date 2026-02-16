"""Output formatting, TTY prompts, and colors."""

import os
import sys

# ANSI color codes — disabled when stdout is not a TTY
_USE_COLOR = hasattr(sys.stdout, "isatty") and sys.stdout.isatty()

RED = "\033[0;31m" if _USE_COLOR else ""
GREEN = "\033[0;32m" if _USE_COLOR else ""
YELLOW = "\033[1;33m" if _USE_COLOR else ""
BLUE = "\033[0;34m" if _USE_COLOR else ""
NC = "\033[0m" if _USE_COLOR else ""


def print_header(msg: str) -> None:
    print(f"\n{BLUE}{'=' * 51}{NC}")
    print(f"{BLUE}  {msg}{NC}")
    print(f"{BLUE}{'=' * 51}{NC}\n")


def print_success(msg: str) -> None:
    print(f"{GREEN}\u2713{NC} {msg}")


def print_error(msg: str) -> None:
    print(f"{RED}\u2717{NC} {msg}", file=sys.stderr)


def print_warning(msg: str) -> None:
    print(f"{YELLOW}\u26a0{NC} {msg}")


def print_info(msg: str) -> None:
    print(f"{BLUE}\u2139{NC} {msg}")


# ---------------------------------------------------------------------------
# TTY input helpers — open /dev/tty so prompts work when stdin is piped
# (curl | bash scenario). Falls back to sys.stdin if /dev/tty is unavailable.
# ---------------------------------------------------------------------------

def _tty_input(prompt_text: str) -> str:
    """Read a line from /dev/tty (or stdin as fallback)."""
    try:
        tty = open("/dev/tty", "r")
    except OSError:
        tty = sys.stdin

    try:
        sys.stderr.write(prompt_text)
        sys.stderr.flush()
        return tty.readline().rstrip("\n")
    finally:
        if tty is not sys.stdin:
            tty.close()


def prompt_yes_no(prompt: str, default: str = "n") -> bool:
    """Prompt for y/n confirmation. Returns True for yes."""
    suffix = " [Y/n]: " if default == "y" else " [y/N]: "
    response = _tty_input(prompt + suffix).strip()
    if not response:
        response = default
    return response.lower() in ("y", "yes")


def prompt_input(prompt: str, default: str = "") -> str:
    """Prompt for a text value with optional default."""
    if default:
        text = f"{prompt} [{default}]: "
    else:
        text = f"{prompt}: "
    response = _tty_input(text).strip()
    return response if response else default
