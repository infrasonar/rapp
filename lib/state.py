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
from configobj import ConfigObj
from typing import Set, List, Dict
from .docker import Docker
from .envvars import (
    COMPOSE_FILE, CONFIG_FILE, ENV_FILE, USE_DEVELOPMENT, PROJECT_NAME,
    DATA_PATH, ALLOW_REMOTE_ACCESS)
from .logview import LogView

RE_VAR = re.compile(r'^[_a-zA-Z][_0-9a-zA-Z]{0,40}$')
RE_TOKEN = re.compile(r'^[0-9a-f]{32}$')
RE_NUMBER = re.compile(r'^([1-9][0-9]*)?$')
RE_WHITE_SPACE = re.compile(r'\s+')

MAX_RA = 3600*24*3  # Max open for 3 days

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
    'ra',
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
    'image': 'selenium/standalone-chrome',
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
    running: Set[str] = set()
    loggers: Dict[str, LogView] = {}

    @classmethod
    async def _init(cls):
        # Overwrite API_URI when using development environment
        if USE_DEVELOPMENT:
            api_url = 'https://devapi.infrasonar.com'
            for agent in _AGENTS.values():
                agent['environment']['API_URI'] = api_url

    @classmethod
    async def get_log(cls, name: str, start: int = 0):
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
    def _read(cls):
        with open(COMPOSE_FILE, 'r') as fp:
            cls.compose_data = yaml.safe_load(fp)
            cls.x_infrasonar_template = \
                cls.compose_data['x-infrasonar-template']

        # remove watchtower from compose file (if required)
        # this can be removed once we are sure that watchtower is removed
        # from all appliances and old installers are no longer used
        cls.clean_watchtower()

        # patch RAPP with ALLOW_REMOTE_ACCESS
        rapp = cls.compose_data['services']['rapp']
        rapp['environment']['ALLOW_REMOTE_ACCESS'] = int(ALLOW_REMOTE_ACCESS)

        with open(CONFIG_FILE, 'r') as fp:
            cls.config_data = yaml.safe_load(fp)

        if not isinstance(cls.config_data, dict):
            # may be None when empty config
            logging.warning('no configurations found')
            cls.config_data = {}
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

        return {
            'probes': probes,
            'agents': agents,
            'configs': configs,
            'agent_token': bool(cls.env_data['AGENT_TOKEN']),
            'agentcore_token': bool(cls.env_data['AGENTCORE_TOKEN']),
            'agentcore_zone_id': cls.env_data['AGENTCORE_ZONE_ID'],
            'socat_target_addr': cls.env_data['SOCAT_TARGET_ADDR'],
            'ra': ra,
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
                assert isinstance(compose, dict), \
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
        probes: List[dict] = state['probes']
        agents: Dict[str, dict] = {
            agent['key']: agent['compose']
            for agent in state['agents']
            if agent['enabled']
        }
        configs: List[dict] = state['configs']
        services: Dict[str, dict] = cls.compose_data['services']

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
