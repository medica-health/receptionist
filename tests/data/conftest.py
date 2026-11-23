import asyncio
import selectors
import sys


def pytest_asyncio_loop_factories(config, item):
    # psycopg async cannot run on the Windows default ProactorEventLoop.
    if sys.platform == "win32":
        return {"selector": lambda: asyncio.SelectorEventLoop(selectors.SelectSelector())}
    return {"default": asyncio.new_event_loop}
