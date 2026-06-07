#!/usr/bin/env python3
"""Oscar — AI Crypto Trading Dashboard.

Usage:
    python main.py           # start web server on http://localhost:8000
    python main.py --bot     # start Telegram-only bot (legacy mode)
"""

import logging
import sys

logging.basicConfig(
    format="%(asctime)s  %(levelname)-8s  %(name)s: %(message)s",
    level=logging.INFO,
)

if __name__ == "__main__":
    if "--bot" in sys.argv:
        from oscar.bot import run_bot
        run_bot()
    else:
        import uvicorn
        uvicorn.run("server:app", host="0.0.0.0", port=8000, reload=False)
