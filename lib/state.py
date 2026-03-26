from __future__ import annotations
import os
import aiohttp
import asyncio
import copy
import datetime
import logging
import os
import re
import time
import yaml
import random
import string
from collections import defaultdict
from configobj import ConfigObj
from typing import TYPE_CHECKING
from .docker import Docker
from .envvars import (
    COMPOSE_FILE, CONFIG_FILE, ENV_FILE, USE_DEVELOPMENT, PROJECT_NAME,
    DATA_PATH, ALLOW_REMOTE_ACCESS, SCRIPTS_FILE)
from .logview import LogView
from .audit import EventId
if TYPE_CHECKING:
    from .rapp import Rapp

RE_VAR = re.compile(r'^[_a-zA-Z][_0-9a-zA-Z]{0,40}$')
RE_TOKEN = re.compile(r'^[0-9a-f]{32}$')
RE_NUMBER = re.compile(r'^([1-9][0-9]*)?$')
RE_WHITE_SPACE = re.compile(r'\s+')
ENV_HEADER = (
    '\n\n'
    'Environment | Value\n'
    '----------- | -----\n'
)


MAX_RA = 3600*24*3  # Max open for 3 days
MAX_RX_SCRIPT_TIMEOUT = 180
RX_HOST = os.getenv('RX_HOST', '127.0.0.1')
RX_PORT = int(os.getenv('RX_PORT', '6214'))

TIME_NULL = '1970-01-01T00:00:00+00:00'

TL = (tuple, list)
COMPOSE_KEYS = set(('environment', 'image'))
PROBE_KEYS = set(('key', 'compose', 'config', 'use', 'enabled'))
AGENT_KEYS = set(('key', 'compose', 'enabled'))
CONFIG_KEYS = set(('like', 'name', 'config'))
STATE_KEYS = set((
    'probes',
    'agents',
    'configs',
    'agent_token',
    'agentcore_token',
    'agentcore_zone_id',
    'socat_target_addr',
    'agentcore',
    'rapp',
    'ra',
    'rx',
))
RA_KEYS = set((
    'allowed',
    'enabled',
    'until',
    'info',
))

LOG_LEVELS = (
    'debug',
    'info',
    'warning',
    'error',
    'critical',
)

AGENT_VARS = {
    'LOG_LEVEL': lambda v: isinstance(v, str) and v.lower() in LOG_LEVELS,
    'LOG_COLORIZED': lambda v: v == 0 or v == 1 or v == '0' or v == '1',
    'ASSET_ID': lambda v: (
        v is None or
        (isinstance(v, int) and v > 0) or
        (isinstance(v, str) and RE_NUMBER.match(v))
    ),
    'NETWORK': lambda v: (
        isinstance(v, str) and v and RE_WHITE_SPACE.match(v) is None
    ),
    'CHECK_NMAP_INTERVAL': lambda v: ((
        isinstance(v, str) and
        RE_NUMBER.match(v) and
        int(v) >= 900 and int(v) <= 259200
    ) or (
        isinstance(v, int) and
        v >= 900 and v <= 259200
    )),
}

AGENTCORE_VARS = {
    'LOG_LEVEL': lambda v: isinstance(v, str) and v.lower() in LOG_LEVELS,
    'LOG_COLORIZED': lambda v: v == 0 or v == 1 or v == '0' or v == '1',
}

RAPP_VARS = {
    'LOG_LEVEL': lambda v: isinstance(v, str) and v.lower() in LOG_LEVELS,
    'LOG_COLORIZED': lambda v: v == 0 or v == 1 or v == '0' or v == '1',
}

RX_VARS = {
    'LOG_LEVEL': lambda v: isinstance(v, str) and v.lower() in LOG_LEVELS,
    'LOG_COLORIZED': lambda v: v == 0 or v == 1 or v == '0' or v == '1',
}

_SOCAT = {
    'image': 'alpine/socat',
    'command': 'tcp-l:443,fork,reuseaddr tcp:${SOCAT_TARGET_ADDR}:443',
    'expose': [443],
    'restart': 'always',
    'logging': {'options': {'max-size': '5m'}},
    'network_mode': 'host'
}

_RA = {
    'image': 'ghcr.io/infrasonar/remote-access',
    'expose': [6213],
    'restart': 'always',
    'logging': {'options': {'max-size': '5m'}},
    'network_mode': 'host',
}

_RX = {
    'image': 'ghcr.io/infrasonar/rapp-rx',
    'restart': 'always',
    'logging': {'options': {'max-size': '5m'}},
    'network_mode': 'host',
}

_DOCKER_AGENT = {
    'environment': {
        'TOKEN': '${AGENT_TOKEN}',
        'API_URI': 'https://api.infrasonar.com'
    },
    'image': 'ghcr.io/infrasonar/docker-agent',
    'volumes': [
        '/var/run/docker.sock:/var/run/docker.sock',
        f'{DATA_PATH}:/data/'
    ]
}

_SPEEDTEST_AGENT = {
    'environment': {
        'TOKEN': '${AGENT_TOKEN}',
        'API_URI': 'https://api.infrasonar.com'
    },
    'image': 'ghcr.io/infrasonar/speedtest-agent'
}

_DISCOVERY_AGENT = {
    'environment': {
        'TOKEN': '${AGENT_TOKEN}',
        'API_URI': 'https://api.infrasonar.com',
        'DAEMON': '1',
        'CONFIG_PATH': '/data/discovery',
    },
    'image': 'ghcr.io/infrasonar/discovery-agent',
    'volumes': [
        '/var/run/docker.sock:/var/run/docker.sock',
        f'{DATA_PATH}:/data/'
    ]
}

_AGENTS = {
    'docker': _DOCKER_AGENT,
    # 'speedtest': _SPEEDTEST_AGENT,  # disable until fixed
    'discovery': _DISCOVERY_AGENT,
}

_SELENIUM = {
    'image': 'ghcr.io/infrasonar/selenium:latest',
    'expose': [4444, 7900],
    'shm_size': '2gb',
    'restart': 'always',
    'logging': {'options': {'max-size': '5m'}},
    'network_mode': 'host',
}


class StateException(Exception):
    pass


class State:
    loop = asyncio.new_event_loop()
    compose_data: dict = {}
    x_infrasonar_template: dict = {}
    env_data: dict = {}
    config_data: dict = {}
    scripts_data: dict = {}
    loggers: dict[str, LogView] = {}
    rapp: Rapp | None = None
    script_locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
    rx_id: int = 0

    @classmethod
    async def _init(cls):
        # Overwrite API_URI when using development environment
        if USE_DEVELOPMENT:
            api_url = 'https://devapi.infrasonar.com'
            for agent in _AGENTS.values():
                agent['environment']['API_URI'] = api_url

    @classmethod
    async def get_log(cls, name: str, start: int = 0) -> dict:
        cname = f'{PROJECT_NAME}-{name}-1'
        logger = cls.loggers.get(cname)
        if logger is None:
            start = 0

            services = await Docker.started_services(running=True)
            if name not in services:
                raise Exception(f'no running services named `{name}`')

            logger = cls.loggers[cname] = LogView(cname, cls.rm_logger)
            await logger.start()

        return logger.get_lines(start)

    @classmethod
    def rm_logger(cls, name: str):
        del cls.loggers[name]

    @classmethod
    def clean_watchtower(cls):
        try:
            # remove watchtower
            del cls.compose_data['services']['watchtower']
        except KeyError:
            pass

        # labels are not used, but they were for watchtower so we remove
        # the labels

        # remove labels from template
        try:
            del cls.x_infrasonar_template['labels']
        except KeyError:
            pass

        # remove those labels
        for service in cls.compose_data.values():
            try:
                del service['labels']
            except KeyError:
                pass

    @classmethod
    def migrate_selenium_to_infrasonar_image(cls):
        try:
            if cls.compose_data['services']['selenium']['image'] == \
                    'selenium/standalone-chrome':
                # In case the original image is used,
                # switch to the InfraSonar image
                cls.compose_data['services']['selenium']['image'] = \
                    _SELENIUM['image']
        except KeyError:
            pass

    @classmethod
    def _read(cls):
        with open(COMPOSE_FILE, 'r') as fp:
            cls.compose_data = yaml.safe_load(fp)
            cls.x_infrasonar_template = \
                cls.compose_data['x-infrasonar-template']

        # remove watchtower from compose file (if required)
        # this can be removed once we are sure that watchtower is removed
        # from all appliances and old installers are no longer used
        cls.clean_watchtower()

        # move to the InfraSonar Selenium image (if required)
        # this can be removed once we are sure that the selenium image is no
        # longer used
        cls.migrate_selenium_to_infrasonar_image()

        # patch RAPP with ALLOW_REMOTE_ACCESS
        rapp = cls.compose_data['services']['rapp']
        rapp['environment']['ALLOW_REMOTE_ACCESS'] = int(ALLOW_REMOTE_ACCESS)

        with open(CONFIG_FILE, 'r') as fp:
            cls.config_data = yaml.safe_load(fp)

        if not isinstance(cls.config_data, dict):
            # may be None when empty config
            logging.warning('no configurations found')
            cls.config_data = {}

        if os.path.exists(SCRIPTS_FILE):
            with open(SCRIPTS_FILE, 'r') as fp:
                scripts_data = yaml.safe_load(fp)

            try:
                assert isinstance(scripts_data, dict), \
                    'no scripts configations found'
                scripts = scripts_data.get('scripts')
                assert isinstance(scripts, list), \
                    '`.scripts` should be a list'
            except Exception as e:
                msg = str(e) or type(e).__name__
                logging.error(f'broken scripts file ({SCRIPTS_FILE}: {msg})')

                # rename broken scripts file
                n, _ = os.path.splitext(SCRIPTS_FILE)
                broken_fn = f'{n}.broken.yaml'
                os.rename(SCRIPTS_FILE, broken_fn)
            else:
                cls.scripts_data = scripts_data
        else:
            cls.scripts_data = {
                'scripts': []
            }

        try:
            conf = ConfigObj(ENV_FILE)
            agentcore_zone_id = \
                int(conf.get('AGENTCORE_ZONE_ID') or 0)  # type: ignore

            cls.env_data = {
                'AGENTCORE_TOKEN': conf['AGENTCORE_TOKEN'],
                'AGENT_TOKEN': conf['AGENT_TOKEN'],
                'AGENTCORE_ZONE_ID': agentcore_zone_id,
                'SOCAT_TARGET_ADDR': conf.get('SOCAT_TARGET_ADDR') or '',
            }
        except Exception as e:
            msg = str(e) or type(e).__name__
            raise Exception(f'broken .env file ({ENV_FILE}: {msg})')

    @classmethod
    def reset_loggers(cls):
        for lv in list(cls.loggers.values()):
            lv.stop()

    @classmethod
    async def update(cls, self_update: bool = False, skip_pull: bool = False):
        await Docker.pull_and_update(
            self_update=self_update,
            skip_pull=skip_pull)

        # reset loggers as the process might be stopped
        cls.reset_loggers()
        # read all
        cls._read()

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
            TMP_FILE = cls.tmp_file(COMPOSE_FILE)
            with open(TMP_FILE, 'w') as fp:
                fp.write(r"""
## InfraSonar docker-compose.yml file
##
## !! This file is managed by InfraSonar !!

""".lstrip())
                yaml.safe_dump(cls.compose_data, fp)
            os.unlink(COMPOSE_FILE)
            os.rename(TMP_FILE, COMPOSE_FILE)
        except Exception as e:
            msg = str(e) or type(e).__name__
            raise Exception(f'failed to write {COMPOSE_FILE} ({msg})')

        try:
            TMP_FILE = cls.tmp_file(CONFIG_FILE)
            with open(TMP_FILE, 'w') as fp:
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
            os.unlink(CONFIG_FILE)
            os.rename(TMP_FILE, CONFIG_FILE)
        except Exception as e:
            msg = str(e) or type(e).__name__
            raise Exception(f'failed to write {CONFIG_FILE} ({msg})')

        try:
            TMP_FILE = cls.tmp_file(SCRIPTS_FILE)
            with open(TMP_FILE, 'w') as fp:
                fp.write(r"""
## WARNING: InfraSonar will make `password` and `secret` values unreadable but
## this must not be regarded as true encryption as the encryption key is
## publicly available.
##
## Example configuration:
##
##  scripts:
##  - name: script.py
##    config:
##      password: "secret password"
##    timeout: 10.0
##    allow_parallel: false
##
## !! This file is managed by InfraSonar !!
##

""".lstrip())
                yaml.safe_dump(cls.scripts_data, fp)
            if os.path.exists(SCRIPTS_FILE):
                os.unlink(SCRIPTS_FILE)
            os.rename(TMP_FILE, SCRIPTS_FILE)
        except Exception as e:
            msg = str(e) or type(e).__name__
            raise Exception(f'failed to write {SCRIPTS_FILE} ({msg})')

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
                if isinstance(v, bool):
                    o = orig.get(k)
                    assert o, f'got a boolean {k} but missing in current state'
                    config[k] = o
                else:
                    assert isinstance(v, str), f'{k} must be boolean or string'

            elif isinstance(v, (tuple, list, set)):
                o = orig.get(k, [])
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
    def get(cls) -> dict:
        agentcore = None
        service = cls.compose_data['services'].get('agentcore')
        if service:
            env = service.get('environment', {})
            env = {k: v for k, v in env.items() if k in AGENTCORE_VARS}
            agentcore = {
                'environment': env
            }

        rapp = None
        service = cls.compose_data['services'].get('rapp')
        if service:
            env = service.get('environment', {})
            env = {k: v for k, v in env.items() if k in RAPP_VARS}
            rapp = {
                'environment': env
            }

        probes = []
        for name, service in cls.compose_data['services'].items():
            if not name.endswith('-probe'):
                continue
            key = name[:-6]
            probe = cls.config_data.get(key, {})
            config = copy.deepcopy(probe.get('config', {}))
            use = probe.get('use', '')
            enabled = probe.get('enabled', True)

            if not enabled:
                # This should be True as we only take services
                logging.warning(
                    f'found probe {key} in compose file while the probe '
                    'should be disabled according the config file')
                continue

            # Make sure to replace passwords and secrets
            cls._replace_secrets(config)

            item = {
                'key': key,
                'compose': {
                    'image': service['image'],
                    'environment': service.get('environment', {}),
                },
                'enabled': enabled,
            }

            if use and isinstance(use, str):
                item['use'] = use
            elif isinstance(config, dict):
                item['config'] = config
            else:
                logging.error(f'invalid config for {name}')
                continue

            probes.append(item)

        for key, probe in cls.config_data.items():
            if not isinstance(probe, dict):
                continue

            config = copy.deepcopy(probe.get('config', {}))
            use = probe.get('use', '')
            enabled = probe.get('enabled', True)
            if enabled is False and probe.get('like') is None:
                # Make sure to replace passwords and secrets
                cls._replace_secrets(config)

                item = {
                    'key': key,
                    'compose': {
                        'image': f'ghcr.io/infrasonar/{key}-probe',
                        'environment': {},
                    },
                    'enabled': enabled,
                }

                if use and isinstance(use, str):
                    item['use'] = use
                elif isinstance(config, dict):
                    item['config'] = config
                else:
                    logging.error(f'invalid config for {key}')
                    continue
                probes.append(item)

        agents = []
        for key in _AGENTS.keys():
            service = cls.compose_data['services'].get(f'{key}-agent')
            if service is None:
                agents.append({
                    'key': key,
                    'enabled': False
                })
            else:
                env = service.get('environment', {})
                env = {k: v for k, v in env.items() if k in AGENT_VARS}

                agents.append({
                    'key': key,
                    'compose': {
                        'image': service['image'],
                        'environment': env
                    },
                    'enabled': True
                })

        configs = []
        for name, obj in cls.config_data.items():
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
                # Make a deep copy
                config = copy.deepcopy(config)
                # Make sure to replace passwords and secrets
                cls._replace_secrets(config)

                item['config'] = config
            else:
                logging.error(f'invalid config for {name}')
                continue

            configs.append(item)

        compose_path, compose_file = os.path.split(COMPOSE_FILE)
        to_name, to_val = ('block', 0) if ALLOW_REMOTE_ACCESS else ('allow', 1)
        ra = {
            'allowed': ALLOW_REMOTE_ACCESS,
            'info': (
                f'To {to_name} remote access, locate '
                f'the `{compose_file}` file at `{compose_path}` on your '
                'appliance and modify the `ALLOW_REMOTE_ACCESS` environment '
                f'variable to `{to_val}` within the rapp service definition '
                'and press _Pull & update_ before making other changes.'
            )
        }
        if ALLOW_REMOTE_ACCESS:
            service_ra = cls.compose_data['services'].get('ra')
            if service_ra is None:
                ra['enabled'] = False
            else:
                until = cls.config_data.get('__ra_until__', TIME_NULL)
                dt = datetime.datetime.fromisoformat(until)
                ra['enabled'] = True
                ra['until'] = int(dt.timestamp())  # type:ignore

        rx = {
            'environment': {},
            'scripts': [{
                'name': script_data['name'],
                'config': {
                    key: True  # set password/secret to boolean
                    for key in script_data.get('config', {})
                },
                'timeout': script_data['timeout'],
                'allow_parallel': script_data['allow_parallel'],
            }
                for script_data in cls.scripts_data['scripts']
            ]
        }
        service_rx = cls.compose_data['services'].get('rx')
        if service_rx is None:
            rx['enabled'] = False
        else:
            env = service_rx.get('environment', {})
            env = {k: v for k, v in env.items() if k in RX_VARS}
            rx['environment'] = env
            rx['enabled'] = True

        return {
            'probes': probes,
            'agents': agents,
            'configs': configs,
            'agent_token': bool(cls.env_data['AGENT_TOKEN']),
            'agentcore_token': bool(cls.env_data['AGENTCORE_TOKEN']),
            'agentcore_zone_id': cls.env_data['AGENTCORE_ZONE_ID'],
            'socat_target_addr': cls.env_data['SOCAT_TARGET_ADDR'],
            'agentcore': agentcore,
            'rapp': rapp,
            'ra': ra,
            'rx': rx,
        }

    @classmethod
    def _sanity_check(cls, state: dict):
        assert isinstance(state, dict), 'expecting state to be a dict'
        probes = state.get('probes')
        assert isinstance(probes, TL), 'probes must be a list in state'
        agents = state.get('agents')
        assert isinstance(agents, TL), 'agents must be a list in state'
        configs = state.get('configs')
        assert isinstance(configs, TL), 'configs must be a list in state'

        probe_keys = [p.get('key') for p in probes if isinstance(p, dict)]
        config_names = [c.get('name') for c in configs if isinstance(c, dict)]
        all_configs = set(probe_keys + config_names)
        assert len(all_configs) == len(probe_keys) + len(config_names), \
            'duplicated probes and/or configs in state'

        for probe in probes:
            assert isinstance(probe, dict), 'probes must be a list with dicts'
            key = probe.get('key')
            assert isinstance(key, str) and RE_VAR.match(key), \
                'missing or invalid `key` in probe'
            enabled = probe.get('enabled', True)
            assert isinstance(enabled, bool), \
                f'invalid `enabled` in probe {key}'

            if enabled:
                compose = probe.get('compose')
                assert isinstance(compose, dict), \
                    f'missing or invalid `compose` in probe {key}'
                image = compose.get('image')
                assert isinstance(image, str) and \
                    image.startswith(f'ghcr.io/infrasonar/{key}-probe'), \
                    f'invalid probe image: {image}'
                environment = compose.get('environment', {})
                assert isinstance(compose, dict), \
                    f'invalid environment for probe {key}'
                for k, v in environment.items():
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
                    isinstance(use, str) and
                    use != key and
                    use in all_configs), \
                    f'invalid "use" value for probe {key}'
                assert config is None or use is None, \
                    f'both "use" and "config" for probe {key}'

            unknown = list(set(probe.keys()) - PROBE_KEYS)
            assert not unknown, f'invalid probe key: {unknown[0]}'

        for agent in agents:
            assert isinstance(agent, dict), 'agents must be a list with dicts'
            key = agent.get('key')
            assert isinstance(key, str) and key in _AGENTS, \
                'missing or invalid `key` in agent'
            enabled = agent.get('enabled')
            assert isinstance(enabled, bool), \
                f'missing or invalid `enabled` in agent {key}'
            compose = agent.get('compose')
            if enabled:
                assert isinstance(compose, dict), \
                    f'invalid `compose` in agent {key}'
                image = compose.get('image')
                assert isinstance(image, str) and \
                    image.startswith(f'ghcr.io/infrasonar/{key}-agent'), \
                    f'invalid agent image: {image}'
                environment = compose.get('environment', {})
                assert isinstance(environment, dict), \
                    f'invalid environment for agent {key}'
                for k, v in environment.items():
                    assert k in AGENT_VARS and AGENT_VARS[k](v), \
                        f'invalid agent environment: {k} = {v}'
                unknown = list(set(compose.keys()) - COMPOSE_KEYS)
                assert not unknown, f'invalid compose key: {unknown[0]}'
            else:
                assert compose is None, \
                    f'unexpected compose; agent {key} is disabled'

            unknown = list(set(agent.keys()) - AGENT_KEYS)
            assert not unknown, f'invalid agent key: {unknown[0]}'

        for config in configs:
            assert isinstance(config, dict), \
                'configs must be a list with dicts'
            like = config.get('like')
            assert isinstance(like, str) and RE_VAR.match(like), \
                'missing or invalid `like` in config'
            name = config.get('name')
            assert isinstance(name, str) and RE_VAR.match(name), \
                'missing or invalid `name` in config'

            cfg = config.get('config')
            assert cfg is None or isinstance(cfg, dict), \
                'config must be a dict'
            if cfg:
                orig = cls.config_data.get(name, {}).get('config', {})
                cls._revert_secrets(cfg, orig)
            use = config.get('use')
            assert use is None or (
                isinstance(use, str) and use != name and use in all_configs), \
                f'invalid "use" value for config {name}'
            assert cfg is None or use is None, \
                f'both "use" and "config" for config {name}'
            assert cfg is not None or use is not None, \
                f'both "use" and "config" missing for config {name}'

            unknown = list(set(config.keys()) - CONFIG_KEYS)
            assert not unknown, f'invalid config name: {unknown[0]}'

        agentcore = state.get('agentcore', {})
        assert isinstance(agentcore, dict), 'agentcore must be a dict'
        agentcore_environment = agentcore.get('environment', {})
        assert isinstance(agentcore_environment, dict), \
            'agentcore environment must be a dict'
        for k, v in agentcore_environment.items():
            assert k in AGENTCORE_VARS and AGENTCORE_VARS[k](v), \
                f'invalid agentcore environment: {k} = {v}'

        rapp = state.get('rapp', {})
        assert isinstance(rapp, dict), 'rapp must be a dict'
        rapp_environment = rapp.get('environment', {})
        assert isinstance(rapp_environment, dict), \
            'rapp environment must be a dict'
        for k, v in rapp_environment.items():
            assert k in RAPP_VARS and RAPP_VARS[k](v), \
                f'invalid rapp environment: {k} = {v}'

        rx = state.get('rx', {})
        assert isinstance(rx, dict), 'rx must be a dict'
        rx_enabled = rx.get('enabled', False)
        assert isinstance(rx_enabled, bool), 'rx enabled must be a boolean'
        rx_environment = rx.get('environment', {})
        assert isinstance(rx_environment, dict), \
            'rx environment must be a dict'
        for k, v in rx_environment.items():
            assert k in RX_VARS and RX_VARS[k](v), \
                f'invalid rx environment: {k} = {v}'

        rx_scripts = rx.get('scripts', [])
        assert isinstance(rx_scripts, TL), 'rx/scripts must be a list'
        for s in rx_scripts:
            assert isinstance(s, dict), 'rx/scripts must be a list with dicts'
            name = s.get('name')
            assert isinstance(name, str), \
                'missing or invalid `name` in script'
            timeout = s.get('timeout')
            assert isinstance(timeout, (float, int)) and (
                timeout > 0 and timeout < MAX_RX_SCRIPT_TIMEOUT), \
                'missing or invalid `timeout` in script'
            allow_parallel = s.get('allow_parallel')
            assert isinstance(allow_parallel, bool), \
                'missing or invalid `allow_parallel` in script'
            cfg = s.get('config')
            assert cfg is None or isinstance(cfg, dict), \
                'script/config must be a dict'
            if cfg:
                for orig in cls.scripts_data['scripts']:
                    if orig['name'] == name:
                        orig_cfg = orig.get('config', {})
                        cls._revert_secrets(cfg, orig_cfg)
                        break

        for token in ('agent_token', 'agentcore_token'):
            t = state.get(token)
            if isinstance(t, str):
                assert RE_TOKEN.match(t), f'invalid {token}'
            elif isinstance(t, bool):
                state[token] = cls.env_data[token.upper()]
            else:
                raise Exception(f'missing or invalid {token}')

        agentcore_zone_id = state.get('agentcore_zone_id')
        assert isinstance(agentcore_zone_id, int) and \
            0 <= agentcore_zone_id <= 9, \
            'missing or invalid `agentcore_zone_id` in state'

        socat_target_addr = state.get('socat_target_addr')
        assert isinstance(socat_target_addr, str), \
            'missing or invalid `socat_target_addr` in state'

        ra = state.get('ra', {})
        assert isinstance(ra, dict), 'ra must be a dict'
        ra_allowed = ra.get('allowed', False)
        assert isinstance(ra_allowed, bool), 'ra/allowed must be a boolean'
        ra_enabled = ra.get('enabled', False)
        assert isinstance(ra_enabled, bool), 'ra/enabled must be a boolean'
        ra_until = ra.get('until', 0)
        assert isinstance(ra_until, int), 'ra/until must be a integer'
        now = int(time.time())
        assert ra_until-now <= MAX_RA, \
            'Remote access can be extended up to 3 days.'

        unknown = list(set(ra.keys()) - RA_KEYS)
        assert not unknown, f'invalid ra key: {unknown[0]}'

        unknown = list(set(state.keys()) - STATE_KEYS)
        assert not unknown, f'invalid state key: {unknown[0]}'

    @classmethod
    def set(cls, state: dict):
        cls._sanity_check(state)
        probes: list[dict] = state['probes']
        agents: dict[str, dict] = {
            agent['key']: agent['compose']
            for agent in state['agents']
            if agent['enabled']
        }
        configs: list[dict] = state['configs']
        services: dict[str, dict] = cls.compose_data['services']

        # remove disabled probes
        for name in list(services.keys()):
            if name.endswith('-probe'):
                key = name[:-6]
                for probe in probes:
                    if probe['key'] == key and probe.get('enabled', True):
                        break
                else:
                    del services[name]

        has_selenium = False

        for probe in probes:
            key = probe["key"]
            enabled = probe.get('enabled', True)
            if not enabled:
                if key in cls.config_data:
                    # Just set enabled to False, this leaves config in tact
                    cls.config_data[key]['enabled'] = False
                else:
                    # Ignore config and use when new
                    cls.config_data[key] = {'enabled': False}
                continue

            compose = probe['compose']
            name = f'{key}-probe'
            if key == 'selenium':
                has_selenium = True
                if 'selenium' not in services:
                    services['selenium'] = _SELENIUM

            if name in services:
                if 'environment' in compose:
                    services[name]['environment'] = compose['environment']
                else:
                    services[name].pop('environment', None)
                services[name]['image'] = compose['image']
            else:
                service = cls.x_infrasonar_template.copy()
                service.update(compose)
                services[name] = service

            use = probe.get('use')
            config = probe.get('config')
            assets = cls.config_data.get(key, {}).get('assets')
            cls.config_data[key] = {'assets': assets} if assets else {}
            if use:
                cls.config_data[key]['use'] = use
            elif config:
                cls.config_data[key]['config'] = config
            elif not assets:
                cls.config_data.pop(key, None)

        if not has_selenium:
            try:
                del services['selenium']
            except KeyError:
                pass

        for key in _AGENTS.keys():
            name = f'{key}-agent'
            if key in agents:
                compose = agents[key]
                if name in services:
                    service = services[name]
                else:
                    service = services[name] = cls.x_infrasonar_template.copy()
                    service.update(_AGENTS[key])

                # skip empty environment variable for agents
                env = compose.get('environment', {})
                service['environment'].update({
                    k: v
                    for k, v in env.items()
                    if v not in ("", None)
                })
                # remove env vars
                for k in [k for k, v in env.items() if v in ("", None)]:
                    service['environment'].pop(k, None)

                service['image'] = compose['image']
            else:
                # disable agent
                services.pop(name, None)

        # get current configs
        configs_to_delete = set([
            name for name, obj in cls.config_data.items()
            if isinstance(obj, dict) and
            obj.get('like') and
            isinstance(obj['like'], str)
        ])

        for config in configs:
            name = config['name']
            use = config.get('use')
            assets = cls.config_data.get(name, {}).get('assets')

            cls.config_data[name] = {'like': config['like']}

            # restore assets if required
            if assets:
                cls.config_data[name]['assets'] = assets

            if use:
                cls.config_data[name]['use'] = use
            else:
                cls.config_data[name]['config'] = config['config']

            # remove from to delete
            try:
                configs_to_delete.remove(name)
            except KeyError:
                pass  # new configs are not in the list

        # remove deleted configs
        for name in configs_to_delete:
            del cls.config_data[name]

        # agentcore
        agentcore = state.get('agentcore', {})
        agentcore_environment = agentcore.get('environment', {})
        if 'agentcore' in services:
            if 'environment' not in services['agentcore']:
                services['agentcore']['environment'] = {}
            services['agentcore']['environment'].update(agentcore_environment)

        # rapp
        rapp = state.get('rapp', {})
        rapp_environment = rapp.get('environment', {})
        if 'rapp' in services:
            if 'environment' not in services['rapp']:
                services['rapp']['environment'] = {}
            services['rapp']['environment'].update(rapp_environment)

        # socat (API forwarder)
        socat_target_addr = state.get('socat_target_addr')
        if socat_target_addr:
            services['socat'] = _SOCAT
        else:
            try:
                del services['socat']
            except KeyError:
                pass

        # remote access
        now = int(time.time())
        ra = state.get('ra', {})
        ra_enabled = ra.get('enabled', False)
        ra_until = ra.get('until', 0)

        if ALLOW_REMOTE_ACCESS and ra_enabled and 55 < ra_until-now <= MAX_RA:
            # only enable when the container is at least active for about
            # one minute from now, otherwise it would be killed almost
            # immediately anyway.
            dt = datetime.datetime.fromtimestamp(ra_until, datetime.UTC)
            cls.config_data['__ra_until__'] = dt.isoformat()
            services['ra'] = _RA
        else:
            try:
                del services['ra']
            except KeyError:
                pass

        # remote execution
        rx = state.get('rx', {})
        rx_enabled = rx.get('enabled', False)
        rx_environment = rx.get('environment', {})
        rx_scripts = rx.get('scripts', [])

        if rx_enabled:
            if 'rx' not in services:
                services['rx'] = _RX.copy()
            if 'environment' not in services['rx']:
                services['rx']['environment'] = {}
            services['rx']['environment'].update(rx_environment)
        else:
            try:
                del services['rx']
            except KeyError:
                pass

        cls.scripts_data = {
            'scripts': [
                s
                for s in sorted(rx_scripts, key=lambda s: s['name'])
            ]
        }

        # update environment variable (all verified with sanity check)
        for key in (
            'agentcore_token',
            'agent_token',
            'agentcore_zone_id',
            'socat_target_addr',
        ):
            cls.env_data[key.upper()] = state[key]

        cls.write()
        asyncio.ensure_future(cls.update())

    @classmethod
    def init(cls):
        cls.loop.run_until_complete(cls._init())

        # Test read
        cls._read()

        # Test get
        cls.get()

        # Test write
        cls.write()

        # Test docker version
        docker_version = cls.loop.run_until_complete(Docker.version())
        logging.info(f'docker version: {docker_version}')

        # Test docker mount (no services found when path does not match)
        services = cls.loop.run_until_complete(Docker.started_services())
        if not services:
            raise Exception(
                'No docker services found. If you are sure docker compose is '
                'running, then most likely the docker mount '
                'does not reflect the path running on the host. Make sure to '
                'verify the COMPOSE_FILE matches the path on the host')

        # ensure stopping the remote access container when required
        asyncio.ensure_future(cls.stop_remote_access_loop(), loop=cls.loop)

    @classmethod
    async def stop_remote_access_loop(cls):
        try:
            while True:
                await asyncio.sleep(5.0)

                ra = cls.compose_data.get('services', {}).get('ra')
                if ra is None:
                    # no remote access container
                    logging.debug('no remote access container active...')
                    continue

                until = cls.config_data.get('__ra_until__', TIME_NULL)
                dt = datetime.datetime.fromisoformat(until)
                if dt >= datetime.datetime.now(datetime.UTC):
                    # remote access expiration in future
                    logging.debug('remote access container active...')
                    continue

                # time is up, stop the container
                logging.info('stop remote access container')

                # remove the service and reset time
                cls.config_data['__ra_until__'] = TIME_NULL
                del cls.compose_data['services']['ra']

                # write compose file
                cls.write()

                # update (skip pull as we only update the state)
                await cls.update(skip_pull=True)

        except asyncio.CancelledError:
            pass

    @staticmethod
    def tmp_file(filename: str) -> str:
        letters = string.ascii_lowercase
        tmp = ''.join(random.choice(letters) for i in range(10))
        return f'{filename}.{tmp}'

    @classmethod
    async def rx(cls, data):
        services = await Docker.started_services(running=True)
        if 'rx' not in services:
            raise Exception('remote execution container not running')

        script_name = data['script']
        for script in cls.scripts_data['scripts']:
            if script.get('name') == script_name:
                break
        else:
            raise Exception(f'script `{script_name}` not found')

        body = data['body']
        env = data['env']
        timeout = script.get('timeout') or 30
        allow_parallel = script.get('allow_parallel') or False
        config = script.get('config', {})
        password = config.get('password')
        secret = config.get('secret')
        if password is not None:
            env['PASSWORD'] = password
        if secret is not None:
            env['SECRET'] = secret

        asyncio.ensure_future(
            cls._rx(script_name, body, env, timeout, allow_parallel)
        )

    @classmethod
    async def _rx(cls, script_name: str, body: str, env: dict[str, str],
                  timeout: int, allow_parallel: bool):
        assert cls.rapp is not None
        rx_id = cls.rx_id
        cls.rx_id += 1

        lock = asyncio.Lock() \
            if allow_parallel \
            else cls.script_locks[script_name]

        async with lock:
            cls.rapp.audit_log({
                'event_id': EventId.RxStart.value,
                'message': f'[{rx_id}]Rx script `{script_name}` started'
            })
            start = time.time()

            url = f'http://{RX_HOST}:{RX_PORT}/run'
            try:
                async with aiohttp.ClientSession(
                    timeout=aiohttp.ClientTimeout(timeout + 10),
                ) as session:
                    async with session.post(url, json={
                        'script': script_name,
                        'body': body,
                        'timeout': timeout,
                        'env': env,
                    }, ssl=False) as resp:
                        resp.raise_for_status()
                        data = await resp.json()
                        error = data.get('error')
            except asyncio.TimeoutError:
                logging.warning(f'script `{script_name}` timed out')
                error = 'Request for RX timeout'
            except Exception as e:
                msg = str(e) or type(e).__name__
                logging.warning(f'script `{script_name}` failed: {msg}')
                error = 'Request for RX failed'
            else:
                if error is None:
                    logging.info(f'script `{script_name}` success')
                else:
                    logging.warning(f'script `{script_name}` error: {error}')

            event = EventId.RxSuccess if error is None else EventId.RxFailed
            env_md = ''
            if env:
                env_body = '\n'.join(f'`{k}` | `{v}`' for k, v in env.items())
                env_md = f'{ENV_HEADER}{env_body}'

            message = (
                f'[{rx_id}]Rx script `{script_name}` success{env_md}'
                if error is None else
                f'[{rx_id}]Rx script `{script_name}` failed: {error}{env_md}'
            )

            duration = time.time() - start
            if duration < 1.1:
                # prevents writing audit log in same second (for order)
                await asyncio.sleep(1.1 - duration)

            cls.rapp.audit_log({
                'event_id': event.value,
                'message': message
            })
