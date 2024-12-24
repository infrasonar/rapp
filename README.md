[![CI](https://github.com/infrasonar/rapp/workflows/CI/badge.svg)](https://github.com/infrasonar/rapp/actions)
[![Release Version](https://img.shields.io/github/release/infrasonar/rapp)](https://github.com/infrasonar/rapp/releases)

# InfraSonar Remote Appliance (RAPP)

## Environment variable

Variable            | Default                        | Description
------------------- | ------------------------------ | ------------
`AGENTCORE_HOST`    | `127.0.0.1`                    | Hostname or Ip address of the AgentCore.
`AGENTCORE_PORT`    | `8770`                         | AgentCore RAPP port to connect to.
`COMPOSE_FILE`      | `/docker/docker-compose.yml`   | Docker compose file.
`CONFIG_FILE`       | `/config/infrasonar.yaml`      | File with probe and asset configuration like credentials.
`LOG_LEVEL`         | `warning`                      | Log level (`debug`, `info`, `warning`, `error` or `critical`).
`LOG_COLORIZED`     | `0`                            | Log using colors (`0`=disabled, `1`=enabled).
`LOG_FTM`           | `%y%m%d %H:%M:%S`              | Log format prefix.

## Docker build

```
docker build -t rapp . --no-cache
```
