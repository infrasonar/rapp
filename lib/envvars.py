import os

AGENTCORE_HOST = os.getenv('AGENTCORE_HOST', '127.0.0.1')
AGENTCORE_PORT = int(os.getenv('AGENTCORE_PORT', 8770))
COMPOSE_FILE = os.getenv('COMPOSE_FILE', '/docker/docker-compose.yml')
ENV_FILE = os.getenv('COMPOSE_FILE', '/docker/.env')
CONFIG_FILE = os.getenv('CONFIG_FILE', '/config/infrasonar.yaml')
COMPOSE_PATH = os.path.dirname(COMPOSE_FILE)