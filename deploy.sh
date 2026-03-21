#!/usr/bin/env bash
# deploy.sh — 在服务器上初次部署 FionaTrade
# 用法：bash deploy.sh
# 适用：Ubuntu 22.04 / Debian 12 (systemd + nginx)
set -euo pipefail

DEPLOY_DIR="/opt/fionatrade"
VENV_DIR="$DEPLOY_DIR/.venv"
SERVICE_USER="fiona"
REPO_URL=""   # 如果用 git 部署，填入你的 repo URL；否则留空（手动上传代码）

echo "=== FionaTrade 部署脚本 ==="

# ── 1. 依赖 ──────────────────────────────────────────────────────────────────
echo "[1/7] 安装系统依赖..."
apt-get update -q
apt-get install -y python3.11 python3.11-venv python3.11-dev \
    git nginx curl sqlite3 build-essential

# ── 2. 用户 ──────────────────────────────────────────────────────────────────
echo "[2/7] 创建系统用户 $SERVICE_USER..."
id -u "$SERVICE_USER" &>/dev/null || useradd --system --shell /bin/bash \
    --create-home --home-dir "$DEPLOY_DIR" "$SERVICE_USER"

# ── 3. 代码 ──────────────────────────────────────────────────────────────────
echo "[3/7] 部署代码到 $DEPLOY_DIR..."
if [ -n "$REPO_URL" ]; then
    if [ -d "$DEPLOY_DIR/.git" ]; then
        git -C "$DEPLOY_DIR" pull
    else
        git clone "$REPO_URL" "$DEPLOY_DIR"
    fi
else
    # 假设你已将项目文件复制到此目录
    echo "  (跳过 git clone — 请确保代码已在 $DEPLOY_DIR)"
fi

chown -R "$SERVICE_USER:$SERVICE_USER" "$DEPLOY_DIR"

# ── 4. Python 虚拟环境 ────────────────────────────────────────────────────────
echo "[4/7] 创建虚拟环境并安装依赖..."
sudo -u "$SERVICE_USER" python3.11 -m venv "$VENV_DIR"
sudo -u "$SERVICE_USER" "$VENV_DIR/bin/pip" install --upgrade pip -q
sudo -u "$SERVICE_USER" "$VENV_DIR/bin/pip" install -e "$DEPLOY_DIR" -q

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
cp "$DEPLOY_DIR/fionatrade.service" /etc/systemd/system/fionatrade.service
systemctl daemon-reload
systemctl enable fionatrade
systemctl restart fionatrade
sleep 3
systemctl status fionatrade --no-pager

# ── 7. nginx ──────────────────────────────────────────────────────────────────
echo "[7/7] 配置 nginx..."
cp "$DEPLOY_DIR/nginx.conf.example" /etc/nginx/sites-available/fionatrade
ln -sf /etc/nginx/sites-available/fionatrade /etc/nginx/sites-enabled/fionatrade
rm -f /etc/nginx/sites-enabled/default
nginx -t && systemctl restart nginx

echo ""
echo "✅ 部署完成！"
echo ""
echo "下一步："
echo "  1. 编辑 $DEPLOY_DIR/.env 填入 API keys"
echo "  2. 修改 /etc/nginx/sites-available/fionatrade 里的 server_name"
echo "  3. (可选) certbot --nginx -d your-domain.com 配置 HTTPS"
echo "  4. systemctl restart fionatrade"
echo "  5. 访问 http://your-server-ip"
