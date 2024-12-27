import re
import asyncio
import logging
from typing import Optional, Tuple, List
from .envvars import COMPOSE_PATH


class DockerException(Exception):
    """Raised when reading the docker version. If this succeeds, other errors
    will be captured and stored."""
    pass


class Docker:

    lock = asyncio.Lock()

    MIN_DOCKER_VERSION = 24
    _RE_DOCKER_VERSION = \
        re.compile(r'Docker version ([0-9]+)\.([0-9]+)\.([0-9]+).*')

    @classmethod
    def _read_docker_version(cls, output) -> Optional[Tuple[int, int, int]]:
        m = cls._RE_DOCKER_VERSION.match(output)
        if not m:
            return
        try:
            major, minor, patch = \
                int(m.group(1)), int(m.group(2)), int(m.group(3))
        except Exception:
            return
        return major, minor, patch

    @classmethod
    async def _run(cls, cmd: str) -> Tuple[str, str]:
        try:
            proc = await asyncio.create_subprocess_shell(
                cmd,
                stderr=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                cwd=COMPOSE_PATH,
            )
            stdout, stderr = await proc.communicate()
            out = stdout.decode()
            err = stderr.decode().strip()
            if err:
                logging.error(err)
        except Exception as e:
            err = str(e) or type(e).__name__
            logging.error(f'cmd `{cmd}` failed (err)')
            return '', err
        else:
            return out, err

    @classmethod
    async def version(cls) -> str:
        async with cls.lock:
            out, err = await cls._run('docker -v')
            if 'not found' in err or 'not found' in out:
                raise DockerException('not found')
            if err:
                raise Exception(err)
            docker_version = cls._read_docker_version(out)
            if not docker_version:
                raise DockerException('missing docker version')
            if docker_version[0] < cls.MIN_DOCKER_VERSION:
                vstr = '.'.join([str(i) for i in docker_version])
                raise DockerException(f'docker too old: v{vstr}')

            return docker_version

    @classmethod
    async def pull_and_update(cls):
        async with cls.lock:
            await cls._run('docker compose pull')
            await cls._run('docker compose up -d --remove-orphans')

    @classmethod
    async def services(cls) -> List[str]:
        async with cls.lock:
            out, err = await cls._run(
                'docker compose ps --services --status running')
            return out.splitlines(keepends=False)
