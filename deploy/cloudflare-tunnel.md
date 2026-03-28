# Cloudflare Tunnel — Deploy Guide

Expose the news-aggregator dashboard to the internet **without opening any ports**
on your VPS. Cloudflare handles HTTPS, DDoS protection, and reverse proxy.

## Architecture

```
Browser → Cloudflare Edge → cloudflared (on VPS) → FastAPI :8000
```

No public IP needed. The tunnel is an outbound connection from your VPS to Cloudflare.

---

## Step 1 — Install cloudflared on VPS

```bash
# Debian/Ubuntu
curl -L https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64.deb \
  -o cloudflared.deb
sudo dpkg -i cloudflared.deb

# Verify
cloudflared --version
```

## Step 2 — Authenticate

```bash
cloudflared tunnel login
# Opens browser → select your Cloudflare zone (domain) → authorize
```

## Step 3 — Create the tunnel

```bash
cloudflared tunnel create news-aggregator
# Saves credentials to ~/.cloudflared/<UUID>.json
# Note the tunnel UUID printed
```

## Step 4 — Configure the tunnel

Create `~/.cloudflared/config.yml`:

```yaml
tunnel: <YOUR_TUNNEL_UUID>
credentials-file: /root/.cloudflared/<YOUR_TUNNEL_UUID>.json

ingress:
  # Main dashboard + API
  - hostname: news.yourdomain.com
    service: http://localhost:8000

  # Weaviate REST API (optional — only if you need external RAG access)
  # - hostname: weaviate.yourdomain.com
  #   service: http://localhost:8080

  # Catch-all (required)
  - service: http_status:404
```

## Step 5 — Point DNS to tunnel

```bash
cloudflared tunnel route dns news-aggregator news.yourdomain.com
# Creates a CNAME record: news.yourdomain.com → <UUID>.cfargotunnel.com
```

## Step 6 — Run as systemd service

```bash
sudo cloudflared service install
sudo systemctl enable cloudflared
sudo systemctl start cloudflared

# Verify
sudo systemctl status cloudflared
```

## Step 7 — Verify

```bash
curl https://news.yourdomain.com/api/health
```

---

## Cloudflare Access (optional — protect the dashboard)

If the dashboard should not be public, add Zero Trust authentication:

1. Go to Cloudflare Zero Trust → Access → Applications
2. Add application: `news.yourdomain.com`
3. Set policy: Allow specific emails or Google Workspace domain
4. Dashboard now requires login — no code changes needed

---

## Firewall hardening (recommended)

Once tunnel is running, block direct HTTP/HTTPS access to the VPS:

```bash
# Allow only SSH + internal services
sudo ufw default deny incoming
sudo ufw allow ssh
sudo ufw allow from 127.0.0.1 to any port 8000   # FastAPI local only
sudo ufw allow from 127.0.0.1 to any port 8080   # Weaviate local only
sudo ufw allow from 127.0.0.1 to any port 6379   # Redis local only
sudo ufw enable
```

Traffic now flows: Internet → Cloudflare → Tunnel → VPS (localhost only).
