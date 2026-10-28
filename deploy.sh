#!/usr/bin/env bash
# Run on the EC2 instance (Amazon Linux 2023) from inside the tunebox folder:
#   chmod +x deploy.sh && ./deploy.sh <your-s3-bucket-name> [aws-region]
set -euo pipefail

BUCKET_NAME="${1:?Usage: ./deploy.sh <s3-bucket-name> [aws-region]}"
REGION="${2:-ap-south-1}"
APP_DIR="$(cd "$(dirname "$0")" && pwd)"
APP_USER="$(whoami)"

echo "==> Installing packages"
sudo dnf install -y python3-pip nginx

echo "==> Creating virtual environment"
python3 -m venv "$APP_DIR/venv"
"$APP_DIR/venv/bin/pip" install --quiet -r "$APP_DIR/requirements.txt"

echo "==> Writing settings (/etc/tunebox.env)"
SECRET="$(python3 -c 'import secrets; print(secrets.token_hex(32))')"
sudo tee /etc/tunebox.env >/dev/null <<ENV
BUCKET_NAME=$BUCKET_NAME
AWS_REGION=$REGION
SECRET_KEY=$SECRET
DB_PATH=$APP_DIR/tunebox.db
ENV
sudo chmod 600 /etc/tunebox.env

echo "==> Creating systemd service"
sudo tee /etc/systemd/system/tunebox.service >/dev/null <<UNIT
[Unit]
Description=Tunebox music sharing app
After=network.target

[Service]
User=$APP_USER
WorkingDirectory=$APP_DIR
EnvironmentFile=/etc/tunebox.env
ExecStart=$APP_DIR/venv/bin/gunicorn -w 2 --timeout 120 -b 127.0.0.1:8000 app:app
Restart=always

[Install]
WantedBy=multi-user.target
UNIT

echo "==> Configuring Nginx reverse proxy"
sudo tee /etc/nginx/conf.d/tunebox.conf >/dev/null <<'NGINX'
server {
    listen 80 default_server;
    server_name _;
    client_max_body_size 30m;
    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_read_timeout 120s;
    }
}
NGINX

sudo systemctl daemon-reload
sudo systemctl enable --now tunebox
sudo nginx -t
sudo systemctl enable nginx
sudo systemctl restart nginx

echo
echo "Done. Open http://<your-ec2-public-ip> in a browser."
echo "Check the app with: sudo systemctl status tunebox"
