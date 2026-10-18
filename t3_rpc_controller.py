#!/usr/bin/env python3
"""Compatibility launcher. Prefer the installed t3ctl command."""

from t3_controller.cli import main


if __name__ == "__main__":
    raise SystemExit(main())
