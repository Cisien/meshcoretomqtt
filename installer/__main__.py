"""Entry point: python3 -m installer <subcommand> [options]"""

import argparse
import os
import sys

from . import InstallerContext
from .system import require_root


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="mctomqtt-installer",
        description="MeshCore to MQTT installer",
    )
    parser.add_argument("--repo", default=os.environ.get("INSTALL_REPO", "Cisien/meshcoretomqtt"))
    parser.add_argument("--branch", default=os.environ.get("INSTALL_BRANCH", "main"))

    sub = parser.add_subparsers(dest="command")

    install_p = sub.add_parser("install", help="Fresh installation")
    install_p.add_argument("--config", dest="config_url", default="", help="URL to download 00-user.toml from")
    install_p.add_argument("--update", action="store_true", help="Non-interactive update mode")

    sub.add_parser("update", help="Update existing installation")

    sub.add_parser("migrate", help="Migrate legacy ~/.meshcoretomqtt installation")

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        sys.exit(1)

    require_root()

    ctx = InstallerContext(
        repo=args.repo,
        branch=args.branch,
        local_install=os.environ.get("LOCAL_INSTALL", ""),
    )

    if args.command == "install":
        ctx.config_url = args.config_url
        ctx.update_mode = args.update
        from .install_cmd import run_install
        run_install(ctx)

    elif args.command == "update":
        ctx.update_mode = True
        from .update_cmd import run_update
        run_update(ctx)

    elif args.command == "migrate":
        from .migrate_cmd import run_migrate
        if not run_migrate(ctx):
            print("No legacy installation found to migrate.")
            sys.exit(0)


if __name__ == "__main__":
    main()
