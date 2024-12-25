import os
import asyncio
import yaml
import logging
from typing import Set, Optional
from configobj import ConfigObj
from .helpers import read_docker_version

COMPOSE_FILE = os.getenv('COMPOSE_FILE', '/docker/docker-compose.yml')
ENV_FILE = os.getenv('COMPOSE_FILE', '/docker/.env')
CONFIG_FILE = os.getenv('CONFIG_FILE', '/config/infrasonar.yaml')
TL = (tuple, list)
COMPOSE_KEYS = set(('environment', 'image'))
PROBE_KEYS = set(('key', 'compose', 'config', 'use'))
AGENT_KEYS = set(('key', 'compose', 'enabled'))
STATE_KEYS = set((
    'probes',
    'agents',
    'configs',
    'agent_token',
    'agentcore_token',
    'agentcore_zone_id',
    'socat_target_addr',
))

class StateException(Exception):
    pass


class State:
    loop = asyncio.new_event_loop()
    lock: Optional[asyncio.Lock] = None
    compose_data: dict = {}
    env_data: dict = {}
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
        try:
            conf = ConfigObj(ENV_FILE)
            cls.env_data = {
                'AGENTCORE_TOKEN': conf['AGENTCORE_TOKEN'],
                'AGENT_TOKEN': conf['AGENT_TOKEN'],
                'AGENTCORE_ZONE_ID': int(conf.get('AGENTCORE_ZONE_ID') or 0),
                'SOCAT_TARGET_ADDR': conf.get('SOCAT_TARGET_ADDR') or '',
            }
        except Exception as e:
            msg = str(e) or type(e).__name__
            raise Exception(f'broken .env file ({ENV_FILE}: {msg})')

    @classmethod
    def write(cls):
        try:
            conf = ConfigObj()
            conf.filename = ENV_FILE
            for k, v in cls.env_data.items():
                conf[k] = v
            conf.write()
        except Exception as e:
            msg = str(e) or type(e).__name__
            raise Exception(f'failed to write {ENV_FILE} ({msg})')

        try:
            with open(COMPOSE_FILE, 'w') as fp:
                fp.write(r"""
## InfraSonar docker-compose.yml file
##
## !! This file is managed by InfraSonar !!

""".lstrip())
                yaml.safe_dump(cls.compose_data, fp)
        except Exception as e:
            msg = str(e) or type(e).__name__
            raise Exception(f'failed to write {COMPOSE_FILE} ({msg})')

        try:
            with open(CONFIG_FILE, 'w') as fp:
                fp.write(r"""
## WARNING: InfraSonar will make `password` and `secret` values unreadable but
## this must not be regarded as true encryption as the encryption key is
## publicly available.
##
## Example configuration for `myprobe` collector:
##
##  myprobe:
##    config:
##      username: alice
##      password: "secret password"
##    assets:
##    - id: [12345, 34567]
##      config:
##        username: bob
##        password: "my secret"
##
## !! This file is managed by InfraSonar !!
##
## It's okay to add custom probe configuration for when you want to
## specify the "_use" value for assets. The appliance toolktip will not
## overwrite these custom probe configurations. You can also add additional
## assets configurations for managed probes.

""".lstrip())
                yaml.safe_dump(cls.config_data, fp)
        except Exception as e:
            msg = str(e) or type(e).__name__
            raise Exception(f'failed to write {CONFIG_FILE} ({msg})')


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
    def _revert_secrets(cls, config: dict, orig: dict):
        for k, v in config.items():
            if k in ('password', 'secret'):
                if isinstance(k, bool):
                    o = orig.get(k)
                    assert o, f'got a boolean {k} but missing in current state'
                    config[k] = o
                else:
                    assert isinstance(v, str), f'{k} must be boolean or string'

            elif isinstance(v, (tuple, list, set)):
                o = orig.get('k', [])
                for idx, i in enumerate(v):
                    if isinstance(i, dict):
                        try:
                            o = o[idx]
                            assert isinstance(o, dict)
                        except Exception:
                            o = {}
                        cls._revert_secrets(i, o)
            elif isinstance(v, dict):
                o = orig.get(k)
                if not isinstance(o, dict):
                    o = {}
                cls._revert_secrets(v, o)

    @classmethod
    def get(cls):
        probes = []
        for name, service in cls.compose_data['services'].items():
            if name.endswith('-probe'):
                continue
            key = name[:-6]
            probe = cls.config_data.get(key, {})
            config = probe.get('config', {})
            use = probe.get('use', '')

            # Make sure to replace passwords and secrets
            cls._replace_secrets(config)

            item = {
                'key': key,
                'compose': {
                    'image': service['image'],
                    'environment': service.get('environment', {}),
                }
            }

            if use and isinstance(use, str):
                item['use'] = use
            elif isinstance(config, dict):
                item['config'] = config
            else:
                logging.error(f'invalid config for {name}')
                continue

            probes.append(item)

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

        configs = []
        for name, obj in cls.config_data:
            if not isinstance(obj, dict):
                continue

            like = obj.get('like')
            if not like or not isinstance(like, str):
                continue

            config = obj.get('config', {})
            use = obj.get('use', '')

            item = {
                "like": like,
                "name": name,
            }

            if use and isinstance(use, str):
                item['use'] = use
            elif isinstance(config, dict):
                item['config'] = config
            else:
                logging.error(f'invalid config for {name}')
                continue

            configs.append(item)

        return {
            'probes': probes,
            'agents': agents,
            'configs': configs,
            'agent_token': bool(cls.env_data['AGENT_TOKEN']),
            'agentcore_token': bool(cls.env_data['AGENTCORE_TOKEN']),
            'agentcore_zone_id': cls.env_data['AGENTCORE_ZONE_ID'],
            'socat_target_addr': cls.env_data['SOCAT_TARGET_ADDR'],
        }


    @classmethod
    def set(cls, state: dict):
        assert isinstance(state, dict), 'expecting state to be a dict'
        probes = state.get('probes')
        assert isinstance(probes, TL), 'probes must be a list in state'
        probe_keys = [p.get('key') for p in probes if isinstance(p, dict)]
        for probe in probes:
            assert isinstance(probe, dict), 'probes must be a list with dicts'
            key = probe.get('key')
            assert key and isinstance(key, str), 'missing or invalid probe key'
            compose = probe.get('compose')
            assert isinstance(compose, dict), \
                f'missing or invalid `compose` in probe {key}'
            image = probe.get('image')
            assert isinstance(image, str) and \
                image.startswith(f'ghcr.io/infrasonar/{key}-probe'), \
                    f'invalid probe image: {image}'
            environment = compose.get('environment', {})
            assert isinstance(compose, dict), \
                f'invalid environment for probe {key}'
            for k, v in environment:
                assert isinstance(k, str) and k and k.upper() == k, \
                    "environment keys must be uppercase strings"
                assert isinstance(v, (int, float, str)), \
                    "environment variable must be number or string"
            unknown = list(set(compose.keys()) - COMPOSE_KEYS)
            assert not unknown, f'invalid compose key: {unknown[0]}'
            config = probe.get('config')
            assert config is None or isinstance(config, dict), \
                'probe config must be a dict'
            if config:
                orig = cls.config_data.get(key, {}).get('config', {})
                cls._revert_secrets(config, orig)
            use = probe.get('use')
            assert use is None or (
                isinstance(use, str) and use != key and use in probe_keys), \
                    f'invalid "use" value for probe {key}'
            assert config is None or use is None, \
                f'both "use" and "config" for probe {key}'
            unknown = list(set(probe.keys()) - PROBE_KEYS)
            assert not unknown, f'invalid probe key: {unknown[0]}'

        agents = state.get('agents')
        assert isinstance(agents, TL), 'agents must be a list in state'
        for agent in agents:
            assert isinstance(agent, dict), 'agents must be a list with dicts'
            key = agent.get('key')
            assert key and isinstance(key, str), 'missing or invalid agent key'
            enabled = agent.get('enabled')
            assert isinstance(enabled, bool), \
                f'missing or invalid `enabled` in agent {key}'

            compose = agent.get('compose')
            assert isinstance(compose, dict), \
                f'missing or invalid `compose` in agent {key}'
            image = agent.get('image')
            assert isinstance(image, str) and \
                image.startswith(f'ghcr.io/infrasonar/{key}-agent'), \
                    f'invalid agent image: {image}'
            environment = compose.get('environment', {})
            assert isinstance(compose, dict), \
                f'invalid environment for agent {key}'
            for k, v in environment:
                assert isinstance(k, str) and k and k.upper() == k, \
                    "environment keys must be uppercase strings"
                assert isinstance(v, (int, float, str)), \
                    "environment variable must be number or string"
            unknown = list(set(compose.keys()) - COMPOSE_KEYS)
            assert not unknown, f'invalid compose key: {unknown[0]}'


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
