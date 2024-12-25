import re
import asyncio
from typing import Optional, Tuple


class DockerException(Exception):
    pass


_MIN_DOCKER_VERSION = 24
_RE_DOCKER_VERSION = \
    re.compile(r'Docker version ([0-9]+)\.([0-9]+)\.([0-9]+).*')


def _read_docker_version(output) -> Optional[Tuple[int, int, int]]:
    m = _RE_DOCKER_VERSION.match(output)
    if not m:
        return
    try:
        major, minor, patch = int(m.group(1)), int(m.group(2)), int(m.group(3))
    except Exception:
        return
    return major, minor, patch


async def read_docker_version() -> str:
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
        raise DockerException('not found')
    if err:
        raise Exception(err)
    docker_version = read_docker_version(out)
    if not docker_version:
        raise DockerException('missing docker version')
    if docker_version[0] < _MIN_DOCKER_VERSION:
        vstr = '.'.join([str(i) for i in docker_version])
        raise DockerException(f'docker too old: v{vstr}')

    return docker_version
