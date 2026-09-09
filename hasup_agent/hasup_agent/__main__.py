"""Process entrypoint: logging, signal handling, exit codes.

The add-on start script (s6 service) runs ``python -m hasup_agent``. The Supervisor
stops an add-on with SIGTERM, so a clean shutdown on SIGTERM (and SIGINT for local
development) is part of the contract: stop the loops, close the WebSocket, exit 0.
"""

from __future__ import annotations

import asyncio
import logging
import signal
import sys

from .agent import Agent
from .config import AgentSettings, load_settings

_LOG_LEVELS = {
    "trace": logging.DEBUG,
    "debug": logging.DEBUG,
    "info": logging.INFO,
    "notice": logging.INFO,
    "warning": logging.WARNING,
    "error": logging.ERROR,
    "fatal": logging.CRITICAL,
}

logger = logging.getLogger("hasup_agent")


def configure_logging(level_name: str) -> None:
    level = _LOG_LEVELS.get(level_name.strip().lower(), logging.INFO)
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        stream=sys.stdout,
        force=True,
    )
    # aiohttp logs every failed connection attempt at warning level already.
    logging.getLogger("aiohttp").setLevel(max(level, logging.WARNING))


async def run(settings: AgentSettings) -> int:
    agent = Agent(settings)
    loop = asyncio.get_running_loop()
    for signal_name in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(signal_name, agent.request_stop)
    return await agent.run()


def main() -> int:
    settings = load_settings()
    configure_logging(settings.log_level)
    try:
        return asyncio.run(run(settings))
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
