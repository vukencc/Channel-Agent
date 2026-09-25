import asyncio

import config
from core.agent import create_session
from core.log import setup_logging

if __name__ == "__main__":
    setup_logging(config.DEBUG)
    asyncio.run(create_session("test"))