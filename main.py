from setproctitle import setproctitle
from lib.version import __version__ as version
from lib.logger import setup_logger
from lib.rapp import Rapp

if __name__ == '__main__':
    setproctitle(f'InfraSonar Remote Appliance (RAPP) v{version}')
    setup_logger()
    Rapp.start()


