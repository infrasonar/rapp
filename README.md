[![CI](https://github.com/infrasonar/rapp/workflows/CI/badge.svg)](https://github.com/infrasonar/rapp/actions)
[![Release Version](https://img.shields.io/github/release/infrasonar/rapp)](https://github.com/infrasonar/rapp/releases)

# InfraSonar Remote Appliance Manager (RAPP)

Use the appliance manager to install the Remote Appliance Manager (RAPP):

![image](https://github.com/user-attachments/assets/8f748331-8e5c-4fb2-ad88-adcab6524232)


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
