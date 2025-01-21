#!/bin/bash

# Create necessary directories with proper permissions
mkdir -p logs data config
chmod 755 logs data config

# Set proper ownership (using current user)
sudo chown -R $USER:$USER logs data config

# Copy example config if it doesn't exist
if [ ! -f config/config.json ]; then
    cp config/config.example.json config/config.json
fi

# Copy example env if it doesn't exist
if [ ! -f .env ]; then
    cp .env.example .env
fi

# Set execute permissions
chmod +x *.sh

# Export current user's UID and GID for docker-compose
echo "UID=$(id -u)" > .env.local
echo "GID=$(id -g)" >> .env.local

echo "Setup complete! You can now run: docker-compose up -d"
