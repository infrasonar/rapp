import logging
import time
import asyncio
from typing import Callable
from .net.package import Package
from .net.protocol import Protocol
from .state import State




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

    def _on_ping(self, pkg: Package):
        logging.debug("Ping")
        pkg = Package.make(self.PROTO_RAPP_RES, pid=pkg.pid, is_binary=True)
        self.write(pkg)

    def _on_read(self, pkg: Package) -> Package:
        logging.debug("Read")
        data = State.get()
        pkg = Package.make(self.PROTO_RAPP_RES, data=data, pid=pkg.pid)
        self.write(pkg)

    def _on_push(self, pkg: Package) -> Package:
        logging.debug("Push")
        try:
            State.set(pkg.data)
        except Exception as e:
            data = {
                'reason': str(e) or f'unknown error: {type(e).__name__}'
            }
            pkg = Package.make(self.PROTO_RAPP_ERR, data=data, pid=pkg.pid)
        else:
            pkg = \
                Package.make(self.PROTO_RAPP_RES, pid=pkg.pid, is_binary=True)
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
        else:
            if State.lock.locked():
                pkg = Package.make(
                    self.PROTO_RAPP_BUSY,
                    pid=pkg.pid,
                    is_binary=True)
                self.write(pkg)
            else:
                handle(self, pkg)

