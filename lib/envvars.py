import os

AGENTCORE_HOST = os.getenv('AGENTCORE_HOST', '127.0.0.1')
AGENTCORE_PORT = int(os.getenv('AGENTCORE_PORT', 8770))
COMPOSE_FILE = os.getenv('COMPOSE_FILE', '/docker/docker-compose.yml')
ENV_FILE = os.getenv('ENV_FILE', '/docker/.env')
CONFIG_FILE = os.getenv('CONFIG_FILE', '/config/infrasonar.yaml')
COMPOSE_PATH = os.path.dirname(COMPOSE_FILE)
USE_DEVELOPMENT = bool(int(os.getenv('USE_DEVELOPMENT', '0')))
COMPOSE_PROJECT_NAME = os.getenv('COMPOSE_PROJECT_NAME', '')
if COMPOSE_PROJECT_NAME:
    # There is a downside in setting the project name: Docker compose will
    # work, even when the path where the docker compose file is mounted does
    # not reflect the path on the host. But, when used, containers will
    # be re-created as dockers detects this as a changes, resulting in
    # potentially unwanted restarts for containers.
    #  An example COMPOSE_PROJECT_NAME = infrasonar
    COMPOSE_CMD = f'docker compose -p {COMPOSE_PROJECT_NAME} --progress plain'
else:
    COMPOSE_CMD = 'docker compose --progress plain'
