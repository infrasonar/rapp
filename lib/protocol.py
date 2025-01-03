import logging
import asyncio
from .net.package import Package
from .net.protocol import Protocol
from .state import State
from .docker import Docker
from .version import IS_RELEASE_VERSION


class RappProtocol(Protocol):

    PROTO_RAPP_PING = 0x40  # None
    PROTO_RAPP_READ = 0x41  # None
    PROTO_RAPP_PUSH = 0x42  # {..}
    PROTO_RAPP_UPDATE = 0x43  # None
    PROTO_RAPP_LOG = 0x44  # {"name": "wmi-probe", "start": 0}

    PROTO_RAPP_RES = 0x50  # {...} / null
    PROTO_RAPP_NO_AC = 0x51  # null
    PROTO_RAPP_NO_CONNECTION = 0x52  # null
    PROTO_RAPP_BUSY = 0x53  # null
    PROTO_RAPP_ERR = 0x54  # {"reason": "..."}

    def __init__(self):
        super().__init__()

    def _empty_ok(self, pkg: Package) -> Package:
        return Package.make(self.PROTO_RAPP_RES, pid=pkg.pid, is_binary=True)

    async def _on_ping(self, pkg: Package) -> Package:
        logging.debug("Ping")
        return self._empty_ok(pkg)

    async def _on_read(self, pkg: Package) -> Package:
        logging.debug("Read")
        data = State.get()
        return Package.make(self.PROTO_RAPP_RES, data=data, pid=pkg.pid)

    async def _on_push(self, pkg: Package):
        logging.debug("Push")
        State.set(pkg.data)
        return self._empty_ok(pkg)

    async def _on_update(self, pkg: Package):
        logging.debug("Pull & Update")
        asyncio.ensure_future(State.update(self_update=True))
        return self._empty_ok(pkg)

    async def _on_log(self, pkg: Package):
        assert isinstance(pkg.data, dict), 'log request must be a dict'
        name = pkg.data.get('name')
        assert name and isinstance(name, str), 'missing or invalid name'
        start = pkg.data.get('start', 0)
        assert isinstance(start, int) and start >= 0, 'invalid start'
        data = await State.get_log(name, start)
        return Package.make(self.PROTO_RAPP_RES, data=data, pid=pkg.pid)

    async def go(self, handle, pkg):
        if Docker.lock.locked():
            logging.debug(f'Busy ({pkg.tp})')
            pkg = Package.make(
                self.PROTO_RAPP_BUSY,
                pid=pkg.pid,
                is_binary=True)
        else:
            try:
                pkg = await handle(self, pkg)
            except Exception as e:
                reason = str(e) or f'unknown error: {type(e).__name__}'
                if IS_RELEASE_VERSION:
                    logging.error(reason)
                else:
                    logging.exception(reason)
                data = {'reason': reason}
                pkg = Package.make(self.PROTO_RAPP_ERR, data=data, pid=pkg.pid)
        self.write(pkg)

    def on_package_received(self, pkg: Package, _map={
        PROTO_RAPP_PING: _on_ping,
        PROTO_RAPP_READ: _on_read,
        PROTO_RAPP_PUSH: _on_push,
        PROTO_RAPP_UPDATE: _on_update,
        PROTO_RAPP_LOG: _on_log,
    }):
        handle = _map.get(pkg.tp)
        if handle is None:
            logging.error(f'unhandled package type: {pkg.tp}')
            return
        asyncio.ensure_future(self.go(handle, pkg))
