# Docker & Docker Compose Deployment

Run CypherSOL in an isolated container on any local developer machine, self-hosted Linux server, or cloud VM (EC2, Compute Engine, Azure VM, DigitalOcean Droplet, Hetzner, etc.).

## 1. Quick Start with Docker Compose (Recommended)

From the project root:
```bash
docker compose up -d
```

- Open [http://localhost:8080](http://localhost:8080) in your web browser.
- Case data, raw input documents, generated reports, and rotating logs are stored locally in `./data/`.
- To stop the service:
  ```bash
  docker compose down
  ```

---

## 2. Standalone Docker Run

Build the production image:
```bash
docker build -t cyphersol:latest .
```

Run the container mounting a host directory to `/data`:
```bash
docker run -d \
  --name cyphersol \
  --restart unless-stopped \
  -p 8080:8080 \
  -v "$(pwd)/data:/data" \
  -e HOST=0.0.0.0 \
  -e PORT=8080 \
  -e CYPHERSOL_DATA_DIR=/data \
  -e CYPHERSOL_ALLOWED_HOSTS=* \
  -e CYPHERSOL_NO_BROWSER=1 \
  cyphersol:latest
```

Check health:
```bash
curl http://localhost:8080/api/health
```

View rotating application logs:
```bash
docker exec -it cyphersol tail -f /data/logs/cyphersol.log
```
