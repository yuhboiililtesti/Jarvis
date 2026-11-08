"""Allow ``python -m jarvis.evolve`` to run the CLI."""

from .cli import cli_main

if __name__ == "__main__":
    raise SystemExit(cli_main())
