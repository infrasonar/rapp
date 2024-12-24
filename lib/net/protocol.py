import asyncio
import logging
from typing import Union, Optional, Dict, Tuple
from .package import Package


RESPONSE_BIT = 0x80


class Protocol(asyncio.Protocol):

    _connected = False

    def __init__(self):
        super().__init__()
        self._buffered_data = bytearray()
        self._package: Optional[Package] = None
        self.transport: Optional[asyncio.Transport] = None

    def connection_made(self, transport: asyncio.Transport):  # type: ignore
        '''
        override asyncio.Protocol
        '''
        self.transport = transport

    def connection_lost(self, exc: Optional[Exception]):
        '''
        override asyncio.Protocol
        '''
        self.transport = None
        self._package = None
        self._buffered_data.clear()

    def is_connected(self) -> bool:
        return self.transport is not None

    def write(self, pkg: Package) -> None:
        self.transport.write(pkg.to_bytes())

    def data_received(self, data: bytes):
        '''
        override asyncio.Protocol
        '''
        self._buffered_data.extend(data)
        while self._buffered_data:
            size = len(self._buffered_data)
            if self._package is None:
                if size < Package.st_package.size:
                    return None
                self._package = Package(self._buffered_data)
            if size < self._package.total:
                return None
            try:
                self._package.extract_data_from(self._buffered_data)
            except KeyError as e:
                logging.error(f'unsupported package received: {e}')
            except Exception:
                logging.exception('failed to unpack data into a package')
                # empty the byte-array to recover from this error
                self._buffered_data.clear()
            else:
                self.on_package_received(self._package)
            self._package = None

    def on_package_received(self, pkg: Package):
        raise NotImplementedError

    async def _timer(self, pid: int, timeout: Union[float, int]):
        await asyncio.sleep(timeout)
        try:
            future, task = self._requests.pop(pid)
        except KeyError:
            logging.error(f'timed out package id not found: {pid}')
            return None

        future.set_exception(TimeoutError(
            f'request timed out on package id: {pid}'))

    def _get_future(self, pkg: Package) -> Optional[asyncio.Future]:
        future, task = self._requests.pop(pkg.pid, (None, None))
        if future is None:
            logging.error(
                f'got a response on package id {pkg.pid} but the original '
                'request has probably timed-out'
            )
            return None
        if task is not None:
            task.cancel()
        return future
