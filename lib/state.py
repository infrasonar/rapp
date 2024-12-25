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
    agent_vars = set([
        'LOG_LEVEL',
        'LOG_COLORIZED'
    ])

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
    def _replace_secrets(cls, config: dict):
        for k, v in config.items():
            if k in ('password', 'secret'):
                config[k] = bool(config[k])
            elif isinstance(v, (tuple, list, set)):
                for i in v:
                    if isinstance(i, dict):
                        cls._replace_secrets(i)
            elif isinstance(v, dict):
                cls._replace_secrets(v)

    @classmethod
    def get(cls):
        probes = []
        for name, service in cls.compose_data['services'].items():
            if name.endswith('-probe'):
                continue
            key = name[:-6]
            config = cls.config_data.get(key, {})

            # Make sure to replace passwords and secrets
            cls._replace_secrets(config)

            probes.append({
                'key': key,
                'compose': {
                    'image': service['image'],
                    'environment': service.get('environment', {}),
                },
                'config': config
            })
        agents = []
        for key in ('docker', 'speedtest'):
            service = cls.compose_data['services'].get(f'{key}-agent')
            if service is None:
                agents.append({
                    'key': key,
                    'enabled': False
                })
            else:
                env = service.get('environment', {})
                env = {k: v for k, v in env.items() if k in cls.agent_vars}

                agents.append({
                    'key': key,
                    'compose': {
                        'image': service['image'],
                        'environment': env
                    },
                    'enabled': True
                })

        return {
            'probes': probes,
            'agents': agents,

        }


    @classmethod
    def set(cls, data: dict):
        pass


    @classmethod
    def init(cls):
        cls.loop.run_until_complete(cls._init())

        # Test read
        cls._read()

        # Test get
        cls.get()

        # Test docker version
        docker_version = cls.loop.run_until_complete(read_docker_version())
        logging.info(f'docker version: {docker_version}')
