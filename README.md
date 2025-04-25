[![CI](https://github.com/infrasonar/rapp/workflows/CI/badge.svg)](https://github.com/infrasonar/rapp/actions)
[![Release Version](https://img.shields.io/github/release/infrasonar/rapp)](https://github.com/infrasonar/rapp/releases)

# InfraSonar Remote Appliance Manager (RAPP)

The InfraSonar Remote Appliance Manager (RAPP) allows you to orchestrate probes and collectors on our [appliance](https://docs.infrasonar.com/collectors/probes/appliance/)

## Environment variable

Variable              | Default                        | Description
--------------------- | ------------------------------ | ------------
`AGENTCORE_HOST`      | `127.0.0.1`                    | Hostname or Ip address of the AgentCore.
`AGENTCORE_PORT`      | `8770`                         | AgentCore RAPP port to connect to.
`COMPOSE_FILE`        | `/docker/docker-compose.yml`   | Docker compose file.
`ENV_FILE`            | `/docker/.env`                 | Environment file.
`CONFIG_FILE`         | `/config/infrasonar.yaml`      | File with configuration like credentials.
`DATA_PATH`           | `./data`                       | Data path.
`USE_DEVELOPMENT`     | `0`                            | Use the development environment.
`SERVICE_NAME`        | `rapp`                         | Name of the "rapp" service withing the compose file.
`PROJECT_NAME`        | _none_                         | Force a docker compose project name. If not set, we assume the project name is **infrasonar**. _(not recommended to set explicitly)_.
`LOG_LEVEL`           | `warning`                      | Log level (`debug`, `info`, `warning`, `error` or `critical`).
`LOG_COLORIZED`       | `0`                            | Log using colors (`0`=disabled, `1`=enabled).
`LOG_FTM`             | `%y%m%d %H:%M:%S`              | Log format prefix.
`SKIP_IMAGE_PRUNE`    | `0`                            | If enabled, skip `docker image prune -a` to cleanup unused images.
`ALLOW_REMOTE_ACCESS` | `0`                            | Allow remote access (blocked by default).

## Docker build

```
docker build -t rapp . --no-cache
```

## Migrating from appliance installer

The [InfraSonar Appliance Installer](https://github.com/infrasonar/appliance-installer) automatically installs rapp. If you installed the appliance using the appliance manager, please follow the instructions below.

**Update Agentcore**

```
docker compose pull agentcore
docker compose up -d  agentcore
```

**Update Appliance Manager**

```
sudo pip install infrasonar-appliance -U --break-system-packages
```

**Enable Remote Appliance**

Via appliance manager (`sudo appliance`):
  1. Remote appliance
  2. Install Remote Appliance (RAPP)
  3. Yes
  4. Back to main
  5. Save and apply changes
  6. Exit

![image](https://github.com/user-attachments/assets/8f748331-8e5c-4fb2-ad88-adcab6524232)


