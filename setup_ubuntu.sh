#!/bin/bash

# Color definitions
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

echo -e "${GREEN}Starting Binance Bot Setup...${NC}"

# Check if running as root
if [ "$EUID" -eq 0 ]; then 
    echo -e "${RED}Please do not run as root/sudo${NC}"
    exit 1
fi

# Check for Docker
if ! command -v docker &> /dev/null; then
    echo -e "${YELLOW}Docker not found. Installing Docker...${NC}"
    curl -fsSL https://get.docker.com -o get-docker.sh
    sudo sh get-docker.sh
    sudo usermod -aG docker $USER
    rm get-docker.sh
    echo -e "${GREEN}Docker installed successfully${NC}"
    echo -e "${YELLOW}Please log out and back in for Docker permissions to take effect${NC}"
    exit 0
fi

# Check for docker-compose
if ! command -v docker-compose &> /dev/null; then
    echo -e "${YELLOW}Installing docker-compose...${NC}"
    sudo curl -L "https://github.com/docker/compose/releases/download/v2.20.2/docker-compose-$(uname -s)-$(uname -m)" -o /usr/local/bin/docker-compose
    sudo chmod +x /usr/local/bin/docker-compose
fi

# Create required directories
echo "Creating directory structure..."
mkdir -p data/backups logs config
chmod 755 data logs config

# Set proper ownership
export UID=$(id -u)
export GID=$(id -g)
sudo chown -R $UID:$GID data logs config

# Copy example config if needed
if [ ! -f config/config.json ]; then
    if [ -f config/config.example.json ]; then
        cp config/config.example.json config/config.json
        echo -e "${YELLOW}Created config.json from example${NC}"
    else
        echo -e "${RED}Warning: No config.example.json found${NC}"
    fi
fi

# Setup environment file
if [ ! -f .env ]; then
    if [ -f .env.example ]; then
        cp .env.example .env
        echo -e "${YELLOW}Created .env from example${NC}"
        echo -e "${YELLOW}Please edit .env with your settings${NC}"
        # Add UID/GID to .env
        echo "UID=$UID" >> .env
        echo "GID=$GID" >> .env
    else
        echo -e "${RED}Warning: No .env.example found${NC}"
    fi
fi

# Verify data files
touch data/trades.json
echo "{}" > data/trades.json
chmod 660 data/trades.json

# Check Docker permissions
if ! docker ps &> /dev/null; then
    echo -e "${RED}Error: Cannot connect to Docker daemon${NC}"
    echo "Please ensure Docker is running and you have proper permissions"
    echo "Run: sudo usermod -aG docker $USER"
    echo "Then log out and back in"
    exit 1
fi

echo -e "${GREEN}Setup complete!${NC}"
echo -e "${YELLOW}Next steps:${NC}"
echo "1. Edit config/config.json with your settings"
echo "2. Edit .env with your API keys"
echo "3. Run: docker-compose up -d"
echo -e "${GREEN}To view logs: docker-compose logs -f${NC}"
