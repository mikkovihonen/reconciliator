"""CLI entrypoint for Reconciliator."""

from __future__ import annotations

from reconciliator.commands import app, main

__all__ = ["app", "main"]

if __name__ == "__main__":
    main()
