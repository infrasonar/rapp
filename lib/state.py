import os
import asyncio
import yaml
import logging
from typing import Set, Optional
from .helpers import read_docker_version

COMPOSE_FILE = os.getenv('COMPOSE_FILE', '/docker/docker-compose.yml')
CONFIG_FILE = os.getenv('CONFIG_FILE', '/config/infrasonar.yaml')


class StateException(Exception):
    pass


class State:
    loop = asyncio.new_event_loop()
    lock: Optional[asyncio.Lock] = None
    compose_data: dict = {}
    config_data: dict = {}
    running: Set[str] = set()

    @classmethod
    async def _init(cls):
        cls.lock = asyncio.Lock()

    @classmethod
    async def _read(cls):
        with open(COMPOSE_FILE, 'r') as fp:
            cls.compose_data = yaml.safe_load(fp)
        with open(CONFIG_FILE, 'r') as fp:
            cls.config_data = yaml.safe_load(fp)

    @classmethod
    def get(cls):
        probes = []
        for name, service in cls.compose_data['services'].items():
            if not name.endswith('-probe'):
                continue
            key = name[:-6]

    @classmethod
    def set(cls, data: dict):
        pass


    @classmethod
    def init(cls):
        cls._read()
        cls.loop.run_until_complete(cls._init())

        # Test docker version
        docker_version = cls.loop.run_until_complete(read_docker_version())
        logging.info(f'docker version: {docker_version}')
