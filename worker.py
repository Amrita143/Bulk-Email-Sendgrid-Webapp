"""
Durable email queue worker.

Run this as a separate Railway service after the Supabase queue migration:

    python worker.py
"""

import asyncio
import logging
import os

from main import run_queue_worker


logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


if __name__ == "__main__":
    poll_interval = int(os.environ.get("QUEUE_POLL_INTERVAL", "5"))
    try:
        asyncio.run(run_queue_worker(poll_interval=poll_interval))
    except KeyboardInterrupt:
        logger.info("Queue worker stopped")
