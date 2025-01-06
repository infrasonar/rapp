import asyncio
import logging
import re
import copy
import yaml
from configobj import ConfigObj
from typing import Set, List, Dict
from .docker import Docker
from .envvars import COMPOSE_FILE, CONFIG_FILE, ENV_FILE, USE_DEVELOPMENT
from .logview import LogView

RE_VAR = re.compile(r'^[_a-zA-Z][_0-9a-zA-Z]{0,40}$')
RE_TOKEN = re.compile(r'^[0-9a-f]{32}$')

TL = (tuple, list)
COMPOSE_KEYS = set(('environment', 'image'))
PROBE_KEYS = set(('key', 'compose', 'config', 'use'))
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
))

LOG_LEVELS = (
    'debug',
    'info',
    'warning',
    'error',
    'critical'
)

AGENT_VARS = {
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

_DOCKER_AGENT = {
    'environment': {
        'TOKEN': '${AGENT_TOKEN}',
        'API_URI': 'https://api.infrasonar.com'
    },
    'image': 'ghcr.io/infrasonar/docker-agent',
    'volumes': [
        '/var/run/docker.sock:/var/run/docker.sock',
        './data:/data/'
    ]
}

_SPEEDTEST_AGENT = {
    'environment': {
        'TOKEN': '${AGENT_TOKEN}',
        'API_URI': 'https://api.infrasonar.com'
    },
    'image': 'ghcr.io/infrasonar/speedtest-agent'
}

_AGENTS = {
    'docker': _DOCKER_AGENT,
    'speedtest': _SPEEDTEST_AGENT,
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
        cls.lock = asyncio.Lock()

        # Overwrite API_URI when using development environment
        if USE_DEVELOPMENT:
            api_url = 'https://devapi.infrasonar.com'
            _SPEEDTEST_AGENT['environment']['API_URI'] = api_url
            _DOCKER_AGENT['environment']['API_URI'] = api_url

    @classmethod
    async def get_log(cls, name: str, start: int = 0):
        cname = f'infrasonar-{name}-1'
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
    def _read(cls):
        with open(COMPOSE_FILE, 'r') as fp:
            cls.compose_data = yaml.safe_load(fp)
            cls.x_infrasonar_template = \
                cls.compose_data['x-infrasonar-template']
        with open(CONFIG_FILE, 'r') as fp:
            cls.config_data = yaml.safe_load(fp)
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
    async def update(cls, self_update: bool = False):
        await Docker.pull_and_update(self_update=self_update)
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
    def get(cls):
        probes = []
        for name, service in cls.compose_data['services'].items():
            if not name.endswith('-probe'):
                continue
            key = name[:-6]
            probe = cls.config_data.get(key, {})
            config = copy.deepcopy(probe.get('config', {}))
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
                isinstance(use, str) and use != key and use in all_configs), \
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
                    if probe['key'] == key:
                        break
                else:
                    del services[name]

        for probe in probes:
            compose = probe['compose']
            key = probe["key"]
            name = f'{key}-probe'
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

        for key in _AGENTS.keys():
            name = f'{key}-agent'
            if key in agents:
                compose = agents[key]
                if name in services:
                    service = services[name]
                else:
                    service = services[name] = cls.x_infrasonar_template.copy()
                    service.update(_AGENTS[key])

                service['environment'].update(compose.get('environment', {}))
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
