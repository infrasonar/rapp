import os
import asyncio
from typing import Set


COMPOSE_FILE = os.getenv('COMPOSE_FILE', '/docker/docker-compose.yml')
CONFIG_FILE = os.getenv('CONFIG_FILE', '/config/infrasonar.yaml')


class State:
    loop = asyncio.new_event_loop()
    lock = asyncio.Lock()
    compose_data: dict = {}
    config_data: dict = {}
    running: Set[str] = set()

    @classmethod
    def init(cls):
        with open(COMPOSE_FILE, 'r') as fp:
            cls.compose_data = yaml.safe_load(fp)
        with open(INFRASONAR_CONF, 'r') as fp:
            cls.config_data = yaml.safe_load(fp)


    @classmethod
    async def test(cls):
        async with cls.lock:
            cmd = 'docker -v'
            proc = await asyncio.create_subprocess_shell(
                cmd,
                stderr=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
            )

            stdout, stderr = await proc.communicate()
            out = stdout.decode()
            err = stderr.decode()
            if 'not found' in err or 'not found' in out:
                raise DockerNotFound()
            if err:
                raise Exception(err)
            docker_version = read_docker_version(out)
            if not docker_version:
                raise DockerNoVersion()
            if docker_version[0] < _MIN_DOCKER_VERSION:
                raise DockerVersionTooOld(
                    '.'.join([str(i) for i in docker_version]))


