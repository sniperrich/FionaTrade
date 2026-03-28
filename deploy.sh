#!/usr/bin/env bash
# deploy.sh — 在服务器上初次部署 FionaTrade
# 用法：bash deploy.sh
# 适用：Debian 12 (Bookworm) / Debian 11 (Bullseye) — systemd + nginx
set -euo pipefail

if [ "$(id -u)" -ne 0 ]; then
    echo "❌ 请使用 root 执行（例如 sudo bash deploy.sh）"
    exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEPLOY_DIR="${DEPLOY_DIR:-$SCRIPT_DIR}"
VENV_DIR="${VENV_DIR:-$DEPLOY_DIR/.venv}"
SERVICE_USER="${SERVICE_USER:-$(id -un)}"
APP_PORT="${APP_PORT:-6888}"
REPO_URL="${REPO_URL:-}"   # 如果用 git 部署，填入你的 repo URL；否则留空（手动上传代码）

run_as_service_user() {
    if [ "$SERVICE_USER" = "root" ]; then
        "$@"
    else
        sudo -u "$SERVICE_USER" "$@"
    fi
}

echo "=== FionaTrade 部署脚本 ==="
echo "DEPLOY_DIR=$DEPLOY_DIR"
echo "SERVICE_USER=$SERVICE_USER"
echo "APP_PORT=$APP_PORT"

# ── 1. 依赖 ──────────────────────────────────────────────────────────────────
echo "[1/7] 安装系统依赖..."
apt-get update -q
apt-get install -y python3 python3-venv python3-dev \
    git nginx curl sqlite3 build-essential

# ── 2. 用户 ──────────────────────────────────────────────────────────────────
echo "[2/7] 创建系统用户 $SERVICE_USER..."
if ! id -u "$SERVICE_USER" &>/dev/null; then
    useradd --system --shell /bin/bash --create-home "$SERVICE_USER"
fi

# ── 3. 代码 ──────────────────────────────────────────────────────────────────
echo "[3/7] 部署代码到 $DEPLOY_DIR..."
if [ -n "$REPO_URL" ]; then
    if [ -d "$DEPLOY_DIR/.git" ]; then
        git -C "$DEPLOY_DIR" pull
    else
        git clone "$REPO_URL" "$DEPLOY_DIR"
    fi
else
    echo "  (跳过 git clone — 请确保代码已在 $DEPLOY_DIR)"
fi

if [ ! -f "$DEPLOY_DIR/pyproject.toml" ] && [ ! -f "$DEPLOY_DIR/setup.py" ]; then
    echo "❌ $DEPLOY_DIR 不是 Python 项目根目录（缺少 pyproject.toml / setup.py）"
    echo "   请把 deploy.sh 放在项目根目录执行，或指定 DEPLOY_DIR=/你的项目目录"
    exit 1
fi

chown -R "$SERVICE_USER:$SERVICE_USER" "$DEPLOY_DIR"
mkdir -p "$DEPLOY_DIR/logs"
chown "$SERVICE_USER:$SERVICE_USER" "$DEPLOY_DIR/logs"

# ── 4. Python 虚拟环境 ────────────────────────────────────────────────────────
echo "[4/7] 创建虚拟环境并安装依赖..."
if [ ! -d "$VENV_DIR" ]; then
    run_as_service_user python3 -m venv "$VENV_DIR"
fi
run_as_service_user "$VENV_DIR/bin/pip" install --upgrade pip -q
run_as_service_user "$VENV_DIR/bin/pip" install -e "$DEPLOY_DIR" -q

# ── 5. 环境变量 ───────────────────────────────────────────────────────────────
if [ ! -f "$DEPLOY_DIR/.env" ]; then
    echo "[5/7] 创建 .env（从 .env.example 复制）..."
    cp "$DEPLOY_DIR/.env.example" "$DEPLOY_DIR/.env"
    chown "$SERVICE_USER:$SERVICE_USER" "$DEPLOY_DIR/.env"
    chmod 600 "$DEPLOY_DIR/.env"
    echo "  ⚠  请编辑 $DEPLOY_DIR/.env 填入真实的 API keys！"
else
    echo "[5/7] .env 已存在，跳过。"
fi

# ── 6. systemd 服务 ───────────────────────────────────────────────────────────
echo "[6/7] 安装 systemd 服务..."
cat > /etc/systemd/system/fionatrade.service <<EOF
[Unit]
Description=FionaTrade Web Control Plane
After=network.target
Wants=network-online.target

[Service]
Type=simple
User=$SERVICE_USER
Group=$SERVICE_USER
WorkingDirectory=$DEPLOY_DIR
Environment="PATH=$VENV_DIR/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin"
EnvironmentFile=$DEPLOY_DIR/.env
ExecStart=$VENV_DIR/bin/uvicorn app.main:app --host 127.0.0.1 --port $APP_PORT --workers 1 --log-level info --no-access-log
Restart=always
RestartSec=10
StartLimitInterval=120
StartLimitBurst=5
StandardOutput=journal
StandardError=journal
SyslogIdentifier=fionatrade
LimitNOFILE=65536
TimeoutStartSec=60
TimeoutStopSec=30

[Install]
WantedBy=multi-user.target
EOF

cat > /etc/systemd/system/fionatrade-worker.service <<EOF
[Unit]
Description=FionaTrade Worker Supervisor
After=network.target
Wants=network-online.target

[Service]
Type=simple
User=$SERVICE_USER
Group=$SERVICE_USER
WorkingDirectory=$DEPLOY_DIR
Environment="PATH=$VENV_DIR/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin"
EnvironmentFile=$DEPLOY_DIR/.env
ExecStart=$VENV_DIR/bin/python -m app.worker.supervisor
Restart=always
RestartSec=5
StartLimitInterval=120
StartLimitBurst=10
StandardOutput=journal
StandardError=journal
SyslogIdentifier=fionatrade-worker
LimitNOFILE=65536
TimeoutStartSec=60
TimeoutStopSec=30

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable fionatrade
systemctl enable fionatrade-worker
systemctl restart fionatrade
systemctl restart fionatrade-worker
sleep 3
systemctl status fionatrade --no-pager
systemctl status fionatrade-worker --no-pager

# ── 7. nginx ──────────────────────────────────────────────────────────────────
echo "[7/7] 配置 nginx..."
cp "$DEPLOY_DIR/nginx.conf.example" /etc/nginx/sites-available/fionatrade
sed -i "s#http://127.0.0.1:6888#http://127.0.0.1:$APP_PORT#g" /etc/nginx/sites-available/fionatrade
sed -i "s#alias /opt/fionatrade/static/#alias $DEPLOY_DIR/static/#g" /etc/nginx/sites-available/fionatrade
ln -sf /etc/nginx/sites-available/fionatrade /etc/nginx/sites-enabled/fionatrade
rm -f /etc/nginx/sites-enabled/default
nginx -t && systemctl restart nginx

echo ""
echo "✅ 部署完成！应用运行在端口 $APP_PORT (反向代理到 nginx 80)"
echo ""
echo "下一步："
echo "  1. 编辑 $DEPLOY_DIR/.env 填入 API keys"
echo "  2. 修改 /etc/nginx/sites-available/fionatrade 里的 server_name"
echo "  3. (可选) apt install certbot python3-certbot-nginx && certbot --nginx -d your-domain.com"
echo "  4. systemctl restart fionatrade"
echo "  5. 访问 http://your-server-ip"
echo ""
echo "常用命令："
echo "  journalctl -u fionatrade -f          # Web 日志"
echo "  journalctl -u fionatrade-worker -f   # Worker/Supervisor 日志"
echo "  systemctl restart fionatrade         # 重启 Web"
echo "  systemctl restart fionatrade-worker  # 重启 Worker"
echo "  curl http://localhost:$APP_PORT/api/health  # 健康检查"
