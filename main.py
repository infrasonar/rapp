from setproctitle import setproctitle
from lib.version import __version__ as version
from lib.logger import setup_logger
from lib.rapp import Rapp
from lib.state import State


if __name__ == '__main__':
    setproctitle(f'InfraSonar Remote Appliance (RAPP) v{version}')
    setup_logger()
    State.init()
    Rapp.start()


