import asyncio
import logging
import time
from typing import List, Optional, Callable
from .envvars import COMPOSE_PATH
from .docker import Docker


class LogView:

    MAX_UNUSED_TIME = 30.0  # kill after 30 seconds unused

    def __init__(self, name: str, on_stop: Callable):
        self.name = name
        self._lines: List[str] = []
        self._process: Optional[asyncio.subprocess.Process] = None
        self._reader: Optional[asyncio.Future] = None
        self._watcher: Optional[asyncio.Future] = None
        self._on_stop = on_stop
        self._accessed: float = 0.0

    async def start(self, n: Optional[int] = None):
        async with Docker.lock:
            tail = f' -n {n}' if n is not None else ''
            cmd = f'docker logs {self.name} -f{tail}'
            self._accessed = time.time()
            logging.info(cmd)
            self._process = await asyncio.create_subprocess_shell(
                cmd,
                stderr=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                cwd=COMPOSE_PATH)
            self._reader = asyncio.ensure_future(self._read())
            self._watcher = asyncio.ensure_future(self._watch())
        await asyncio.sleep(0.5)  # give a little time to read some lines

    async def _read(self):
        try:
            while True:
                if self._process is None or self._process.stderr is None:
                    break
                line = await self._process.stderr.readline()
                if line:
                    try:
                        line = line.decode().strip()
                    except Exception as e:
                        self._lines.append(f'Decoding error: {e}')
                    else:
                        self._lines.append(line)
                else:
                    break
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logging.error(f'failed reading log {self.name}: {e}')
            self.stop()

    async def _watch(self):
        while True:
            now = time.time()
            if now - self._accessed > self.MAX_UNUSED_TIME:
                break
            await asyncio.sleep(1.0)
        self.stop()

    def get_lines(self, start: int = 0, limit=500) -> dict:
        self._accessed = time.time()
        n = c = len(self._lines)
        if start > n:
            start = 0
        if n - start > limit:
            n = start + limit
        return {
            'lines': self._lines[start:n],
            'next': n,
            'count': c,
            'start': start,
            'limit': limit,
        }

    def stop(self):
        try:
            assert self._reader is not None
            self._reader.cancel()
        except Exception:
            pass
        try:
            assert self._watcher is not None
            self._watcher.cancel()
        except Exception:
            pass
        try:
            assert self._process is not None
            self._process.kill()
            logging.info(f'stop logger: {self.name}')

            # below is a fix for Python 3.12 (for some reason close is not
            # reached on the transport after calling kill or terminatre)
            self._process._transport.close()  # type: ignore
        except Exception:
            pass

        self._reader = None
        self._watcher = None
        self._process = None

        self._on_stop(self.name)
        self._on_stop = lambda _: None
