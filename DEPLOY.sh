# Docker Deployment Guide
# Building Code Web Project
# ─────────────────────────────────────────────────────────────


## FILES TO ADD TO YOUR PROJECT ROOT
#
#  your-project/
#  ├── Dockerfile              ← new
#  ├── docker-compose.yml      ← new
#  ├── supervisord.conf        ← new
#  ├── .dockerignore           ← new
#  ├── .streamlit/
#  │   └── config.toml        ← new
#  ├── .env                    ← you create this (never commit)
#  ├── main.py
#  ├── requirements.txt
#  └── ... rest of project


## ─────────────────────────────────────────────────────────────
## PHASE 1 — ON YOUR LOCAL MACHINE
## ─────────────────────────────────────────────────────────────

## Step 1: Run the pipeline locally to generate the JSON output
python main.py bcbc_2024_Part4-509-654.pdf
# This creates: storage/output/structured_document.json
# Do this BEFORE building the Docker image


## Step 2: Create your .env file (never commit this file)
# Create a file called .env in your project root:

DATALAB_API_KEY=your_datalab_key_here
ANTHROPIC_API_KEY=your_anthropic_key_here


## Step 3: Place all the new Docker files into your project root
# Copy Dockerfile, docker-compose.yml, supervisord.conf,
# .dockerignore, and .streamlit/config.toml into your project.


## Step 4: Test Docker locally first (optional but recommended)
docker compose up --build
# Open http://localhost:9901 in your browser
# If it works, press Ctrl+C to stop, then proceed to server deployment


## ─────────────────────────────────────────────────────────────
## PHASE 2 — ON YOUR COMPANY SERVER
## ─────────────────────────────────────────────────────────────

## Step 5: SSH into your server
ssh -i ./quantumai-key root@103.174.189.183 -p 9904


## Step 6: Install Docker on the server
apt update && apt upgrade -y
apt install -y ca-certificates curl gnupg

# Add Docker's official GPG key
install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/ubuntu/gpg \
  | gpg --dearmor -o /etc/apt/keyrings/docker.gpg
chmod a+r /etc/apt/keyrings/docker.gpg

# Add Docker repository
echo \
  "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] \
  https://download.docker.com/linux/ubuntu \
  $(. /etc/os-release && echo "$VERSION_CODENAME") stable" \
  | tee /etc/apt/sources.list.d/docker.list > /dev/null

# Install Docker
apt update
apt install -y docker-ce docker-ce-cli containerd.io docker-compose-plugin

# Verify Docker is running
docker --version
docker compose version


## Step 7: Upload your project to the server
# Run this on your LOCAL machine (not the server):
scp -i ./quantumai-key -P 9904 -r ./your-project-folder \
  root@103.174.189.183:/var/www/buildingcode


## Step 8: Back on the server — go to the project folder
cd /var/www/buildingcode


## Step 9: Create the .env file on the server
nano .env
# Paste your API keys:
#   DATALAB_API_KEY=your_actual_key
#   ANTHROPIC_API_KEY=your_actual_key
# Save: Ctrl+X → Y → Enter


## Step 10: Open the firewall port
ufw allow 9901/tcp
ufw reload


## Step 11: Build and start the Docker container
docker compose up -d --build
# -d means "detached" (runs in background)
# --build forces a fresh build


## Step 12: Check it's running
docker compose ps
# You should see: buildingcode-app   running

docker compose logs -f
# Watch live logs. Press Ctrl+C to stop watching (container keeps running)


## Step 13: Open in browser
# http://103.174.189.183:9901
# Share this link with your client ✅


## ─────────────────────────────────────────────────────────────
## USEFUL COMMANDS (run these on the server)
## ─────────────────────────────────────────────────────────────

## View live logs
docker compose logs -f

## View only Streamlit logs
docker compose logs -f buildingcode | grep streamlit

## Restart the app
docker compose restart

## Stop the app
docker compose down

## Stop and remove everything (including volumes)
docker compose down -v

## Rebuild after code changes
docker compose up -d --build

## Get a shell inside the running container (for debugging)
docker exec -it buildingcode-app bash

## Check how much memory/CPU the container is using
docker stats buildingcode-app


## ─────────────────────────────────────────────────────────────
## UPDATING THE APP AFTER CODE CHANGES
## ─────────────────────────────────────────────────────────────

## On your local machine — upload updated files:
scp -i ./quantumai-key -P 9904 -r ./your-project-folder \
  root@103.174.189.183:/var/www/buildingcode

## On the server — rebuild and restart:
cd /var/www/buildingcode
docker compose up -d --build


## ─────────────────────────────────────────────────────────────
## TROUBLESHOOTING
## ─────────────────────────────────────────────────────────────

## App not loading?
docker compose logs -f
# Look for Python errors or port conflicts

## Port already in use?
ss -tlnp | grep 9901
# If something is using 9901, either stop it or change the port
# in docker-compose.yml (left side of "9901:8501")

## structured_document.json not found?
# Run the pipeline locally first:
#   python main.py bcbc_2024_Part4-509-654.pdf
# Then re-upload and rebuild.

## Container keeps restarting?
docker compose logs buildingcode
# The error message will tell you exactly what's wrong