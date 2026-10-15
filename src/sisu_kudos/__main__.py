"""Command-line launcher."""

from __future__ import annotations

import argparse
from collections.abc import Sequence

from . import __version__
from .config import ConfigurationError, load_settings


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="sisu-kudos", description="Run the KUDOS dashboard")
    parser.add_argument("--config", metavar="PATH", help="read configuration from a TOML file")
    parser.add_argument(
        "--check-config",
        action="store_true",
        help="validate configuration and exit without starting collectors or the server",
    )
    parser.add_argument("--version", action="version", version=f"sisu-kudos {__version__}")
    return parser


def main(argv: Sequence[str] | None = None) -> None:
    args = _parser().parse_args(argv)
    try:
        settings = load_settings(args.config)
    except ConfigurationError as exc:
        _parser().error(str(exc))
    if args.check_config:
        print("Configuration valid.")
        return

    import uvicorn

    from .app import create_app

    uvicorn.run(create_app(settings), host=settings.host, port=settings.port, log_level="info")


if __name__ == "__main__":
    main()
