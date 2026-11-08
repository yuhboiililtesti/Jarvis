"""Allow ``python -m jarvis.evolve`` to delegate to the standalone CLI."""

from evolve.cli import cli_main

if __name__ == "__main__":
    raise SystemExit(cli_main())
