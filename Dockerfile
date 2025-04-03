FROM docker:rc-cli

# Install python/pip
ENV PYTHONUNBUFFERED=1
RUN apk add --update --no-cache python3 && ln -sf python3 /usr/bin/python
RUN rm /usr/lib/python*/EXTERNALLY-MANAGED
RUN python3 -m ensurepip
RUN pip3 install --no-cache --upgrade pip setuptools

# Volume mounts
VOLUME [ "/config" ]
VOLUME [ "/docker" ]
VOLUME [ "/var/run/docker.sock" ]

# Environment variable
ENV AGENTCORE_HOST=127.0.0.1
ENV AGENTCORE_PORT=8770
ENV COMPOSE_FILE=/docker/docker-compose.yml
ENV CONFIG_FILE=/config/infrasonar.yaml
ENV LOG_LEVEL=info
ENV LOG_COLORIZED=1

# Install application
ADD . /code
WORKDIR /code
RUN pip3 install --no-cache-dir -r requirements.txt
CMD ["python3", "main.py"]
