#!/bin/bash

# Source local environment variables
if [ -f .env.local ]; then
    source .env.local
fi

# Build and start the container
docker-compose up --build -d

# Show logs
docker-compose logs -f
