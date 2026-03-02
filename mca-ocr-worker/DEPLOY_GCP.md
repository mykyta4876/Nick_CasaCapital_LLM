# Deploy MCA Underwriting Service on GCP VM

This guide covers deploying the Flask underwriting app and email worker on a Google Cloud Platform (GCP) Linux VM.

## 1. Create a GCP VM

- In **Google Cloud Console** → **Compute Engine** → **VM instances** → **Create instance**.
- Choose a region and machine type (e.g. `e2-medium` or `e2-small`).
- **Boot disk**: Ubuntu 22.04 LTS (or 20.04).
- Under **Identity and API access**: allow **Full access to all Cloud APIs** (or at least no extra scope needed for this app; Gmail uses OAuth).
- Optional: add a **firewall tag** so you can open HTTP/HTTPS in VPC firewall rules.
- Create the VM and note its external IP.

## 2. SSH into the VM and install basics

```bash
# Update and install Python 3.11+
sudo apt-get update
sudo apt-get install -y python3.11 python3.11-venv python3-pip git

# Optional: install nginx as reverse proxy
# sudo apt-get install -y nginx
```

## 3. Deploy the project

Either clone your repo or copy the project onto the VM. The app expects this layout:

```
/home/ubuntu/Nick_CasaCapital_LLM/   # PROJECT_ROOT
├── mca-ocr-worker/
│   ├── src/
│   ├── templates/
│   ├── config/
│   ├── credentials.json   # you must add this
│   ├── token.json         # created on first Gmail auth
│   ├── requirements.txt
│   └── ...
└── casa-capital/
    └── deals/             # deal folders and PDFs go here
```

Example (clone from Git):

```bash
sudo mkdir -p /opt/apps
sudo chown $USER /opt/apps
cd /opt/apps
git clone <your-repo-url> Nick_CasaCapital_LLM
cd Nick_CasaCapital_LLM/mca-ocr-worker
```

If you copy the folder manually, ensure `casa-capital/deals` exists under the same parent as `mca-ocr-worker`:

```bash
mkdir -p /opt/apps/Nick_CasaCapital_LLM/casa-capital/deals
```

## 4. Python environment and dependencies

```bash
cd /opt/apps/Nick_CasaCapital_LLM/mca-ocr-worker

python3.11 -m venv venv
source venv/bin/activate

pip install --upgrade pip
pip install -r requirements.txt
```

If you use Surya OCR for scanned PDFs, install PyTorch and surya-ocr separately (see README).

## 5. Gmail credentials

- Place **credentials.json** (from your Google Cloud project, Gmail API enabled) in `mca-ocr-worker/`:

  ```
  /opt/apps/Nick_CasaCapital_LLM/mca-ocr-worker/credentials.json
  ```

- **First-time Gmail auth**: run the app or fetcher once in a way that can open a browser, or use a headless auth flow:
  - On your **local** machine (with browser), run once:
    - `python src/gmail_fetcher.py --query "has:attachment filename:pdf" --max-results 1`
    - Complete the OAuth flow; this creates `token.json`.
  - Copy `token.json` to the VM at `mca-ocr-worker/token.json`.
  - The VM will then use `token.json` without a browser.

- Restrict permissions on the VM:

  ```bash
  chmod 600 credentials.json token.json
  ```

## 6. Run the Flask app (production)

Use **gunicorn** so the app listens on all interfaces and is suitable for production.

```bash
cd /opt/apps/Nick_CasaCapital_LLM/mca-ocr-worker
source venv/bin/activate

# Listen on 0.0.0.0:8080 (run from mca-ocr-worker so templates and paths resolve)
export PORT=8080
gunicorn --bind 0.0.0.0:${PORT} --workers 1 --threads 4 --pythonpath src "uw_service:app"
```

- `--workers 1` keeps a single process (simpler for file I/O and Gmail token); increase if you need more concurrency.
- To run in the background: use `systemd` (below) or `nohup` / `screen` / `tmux`.

## 7. Run the email worker (background)

The email worker polls unread Gmail every 10 seconds. Run it in a separate process.

```bash
cd /opt/apps/Nick_CasaCapital_LLM/mca-ocr-worker
source venv/bin/activate
cd src
python email_worker.py
```

Run in background with `nohup` or use the systemd unit below.

## 8. Systemd (optional, recommended)

Create two systemd services so the app and worker start on boot and restart on failure.

**Flask app** — `/etc/systemd/system/mca-uw.service`:

```ini
[Unit]
Description=MCA Underwriting Flask App
After=network.target

[Service]
Type=simple
User=ubuntu
WorkingDirectory=/opt/apps/Nick_CasaCapital_LLM/mca-ocr-worker
Environment="PATH=/opt/apps/Nick_CasaCapital_LLM/mca-ocr-worker/venv/bin"
Environment="PORT=8080"
ExecStart=/opt/apps/Nick_CasaCapital_LLM/mca-ocr-worker/venv/bin/gunicorn --bind 0.0.0.0:8080 --workers 1 --threads 4 --pythonpath src uw_service:app
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

**Email worker** — `/etc/systemd/system/mca-email-worker.service`:

```ini
[Unit]
Description=MCA Email Worker (Gmail poller)
After=network.target

[Service]
Type=simple
User=ubuntu
WorkingDirectory=/opt/apps/Nick_CasaCapital_LLM/mca-ocr-worker/src
Environment="PATH=/opt/apps/Nick_CasaCapital_LLM/mca-ocr-worker/venv/bin"
ExecStart=/opt/apps/Nick_CasaCapital_LLM/mca-ocr-worker/venv/bin/python email_worker.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

Then:

```bash
sudo systemctl daemon-reload
sudo systemctl enable mca-uw mca-email-worker
sudo systemctl start mca-uw mca-email-worker
sudo systemctl status mca-uw mca-email-worker
```

Adjust paths and `User` if you use a different user or install path.

## 9. Firewall and HTTPS

- **GCP firewall**: open TCP port **8080** (or the port you use) for the VM’s network tag or IP so you can reach the app.
- **HTTPS**: for production, put **nginx** (or another reverse proxy) in front of gunicorn and terminate TLS, or use a load balancer with HTTPS.

Example nginx upstream (after installing nginx):

```nginx
# /etc/nginx/sites-available/mca-uw
server {
    listen 80;
    server_name YOUR_VM_IP_OR_DOMAIN;
    location / {
        proxy_pass http://127.0.0.1:8080;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
```

Enable and reload:

```bash
sudo ln -s /etc/nginx/sites-available/mca-uw /etc/nginx/sites-enabled/
sudo nginx -t && sudo systemctl reload nginx
```

## 10. Environment variables (reference)

| Variable       | Default    | Description                          |
|----------------|------------|--------------------------------------|
| `PORT`         | `5000`     | Port for gunicorn / Flask.           |
| `FLASK_HOST`   | `127.0.0.1`| Host when running `python uw_service.py` (use `0.0.0.0` to bind all). |
| `FLASK_DEBUG`  | `false`    | Set to `true` for debug mode (dev only). |

For production, run with **gunicorn** and set `PORT`; no need to set `FLASK_HOST` for gunicorn.

## 11. Quick checks

- **Health**: `curl http://VM_EXTERNAL_IP:8080/deals` — should return the deals list page.
- **Logs** (if using systemd):
  - `journalctl -u mca-uw -f`
  - `journalctl -u mca-email-worker -f`
- **Deals directory**: ensure the app user can read/write `PROJECT_ROOT/casa-capital/deals` (e.g. `chown -R ubuntu:ubuntu /opt/apps/Nick_CasaCapital_LLM/casa-capital`).
