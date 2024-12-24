import re
from typing

_RE_DOCKER_VERSION = \
    re.compile(r'Docker version ([0-9]+)\.([0-9]+)\.([0-9]+).*')


def read_docker_version(output) -> Optional[Tuple[int, int, int]]:
    m = _RE_DOCKER_VERSION.match(output)
    if not m:
        return
    try:
        major, minor, patch = int(m.group(1)), int(m.group(2)), int(m.group(3))
    except Exception:
        return
    return major, minor, patch