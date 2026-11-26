#!/usr/bin/env python3
"""Container entry (``python3 /app/app.py``): delegates to the package."""
from rs485_testbench.app import main

if __name__ == "__main__":
    raise SystemExit(main())
