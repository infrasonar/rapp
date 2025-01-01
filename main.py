from setproctitle import setproctitle
from lib.version import __version__ as version
from lib.logger import setup_logger
from lib.rapp import Rapp
from lib.state import State
import logging


if __name__ == '__main__':
    setproctitle('rapp')
    setup_logger()
    logging.warning(f'Starting InfraSonar RAPP v{version}')
    State.init()
    Rapp().start()
