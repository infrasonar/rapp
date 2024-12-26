import asyncio
import logging
import os
import signal
from typing import Optional
from .protocol import RappProtocol
from .state import State
from .envvars import AGENTCORE_HOST, AGENTCORE_PORT


class Rapp:
    def __init__(self):
        self._protocol: Optional[RappProtocol] = None
        self._connecting: bool = False

    def is_connected(self) -> bool:
        return self._protocol is not None and self._protocol.is_connected()

    def is_connecting(self) -> bool:
        return self._connecting

    async def _connect(self):
        conn = State.loop.create_connection(
            RappProtocol,
            host=AGENTCORE_HOST,
            port=AGENTCORE_PORT
        )
        self._connecting = True

        try:
            _, self._protocol = await asyncio.wait_for(conn, timeout=10)
        except Exception as e:
            error_msg = str(e) or type(e).__name__
            logging.error(f'connecting to agentcore failed: {error_msg}')
        finally:
            self._connecting = False

    def _stop(self, signame, *args):
        logging.warning(
            f'signal \'{signame}\' received, stop RAPP')
        for task in asyncio.all_tasks():
            task.cancel()

    async def _start(self):
        initial_step = 2
        step = 2
        max_step = 2 ** 7

        while True:
            if not self.is_connected() and not self.is_connecting():
                asyncio.ensure_future(self._connect(), loop=State.loop)
                step = min(step * 2, max_step)
            else:
                step = initial_step
            for _ in range(step):
                await asyncio.sleep(1)

    def start(self):
        signal.signal(signal.SIGINT, self._stop)
        signal.signal(signal.SIGTERM, self._stop)
        loop = State.loop

        try:
            loop.run_until_complete(self._start())
        except asyncio.exceptions.CancelledError:
            loop.run_until_complete(loop.shutdown_asyncgens())
            loop.close()

    def close(self):
        if self._protocol and self._protocol.transport:
            self._protocol.transport.close()
        self._protocol = None
