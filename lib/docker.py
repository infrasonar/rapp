import asyncio
import logging
import re
import sys
from typing import Optional, Tuple, List
from .envvars import COMPOSE_PATH, COMPOSE_CMD, SERVICE_NAME as SVC_NAME
from .envvars import SKIP_IMAGE_PRUNE
from .logger import LOG_LEVEL


EXCLUDE_SERVICES = set((SVC_NAME, 'watchtower'))


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
            err = stderr.decode()
            if err.strip() and LOG_LEVEL <= logging.WARNING:
                logging.warning('------ Docker out start ------')
                print(err, file=sys.stderr)
                logging.warning('------ Docker out end ------')
        except Exception as e:
            err = str(e) or type(e).__name__
            logging.error(f'cmd `{cmd}` failed (err)')
            return '', err
        else:
            return out, err

    @classmethod
    async def version(cls) -> Tuple[int, int, int]:
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
    async def image_prune(cls):
        out, err = await cls._run('docker image prune -a -f')
        if err.strip() and LOG_LEVEL <= logging.ERROR:
            logging.error('------ Docker err start ------')
            print(err, file=sys.stderr)
            logging.error('------ Docker err end ------')
        elif out.strip() and LOG_LEVEL <= logging.WARNING:
            logging.warning('------ Docker image prune start ------')
            print(out, file=sys.stderr)
            logging.warning('------ Docker image prune end ------')

    @classmethod
    async def pull_and_update(cls,
                              self_update: bool = False,
                              skip_pull: bool = False):
        services = await cls.configured_services()
        services = ' '.join(set(services) - EXCLUDE_SERVICES)
        async with cls.lock:
            if not skip_pull:
                await cls._run(f'{COMPOSE_CMD} pull {services}')

            await cls._run(f'{COMPOSE_CMD} up -d {services} --remove-orphans')
            if not SKIP_IMAGE_PRUNE:
                await asyncio.sleep(1.0)
                await cls.image_prune()

            if self_update:
                # This is a trick, if restarted from this container updating
                # will fail. By starting another container which kicks the
                # update, we can update ourself.
                await cls._run(f'{COMPOSE_CMD} pull {SVC_NAME}')
                cmd = (
                    f"docker run "
                    f"-v {COMPOSE_PATH}:{COMPOSE_PATH} "
                    f"-v /var/run/docker.sock:/var/run/docker.sock "
                    f"--entrypoint '/bin/sh' "
                    f"docker:rc-cli -c "
                    f"'cd {COMPOSE_PATH} && {COMPOSE_CMD} up -d {SVC_NAME}'")
                await cls._run(cmd)

    @classmethod
    async def started_services(cls, running: bool = False) -> List[str]:
        status = ' --status running' if running else ''
        async with cls.lock:
            out, _ = await cls._run(
                f'{COMPOSE_CMD}  ps --services{status}')
            return out.splitlines(keepends=False)

    @classmethod
    async def configured_services(cls) -> List[str]:
        async with cls.lock:
            out, _ = await cls._run(
                f'{COMPOSE_CMD}  config --services')
            return out.splitlines(keepends=False)
