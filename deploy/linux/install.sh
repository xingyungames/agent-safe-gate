#!/usr/bin/env bash
# 报销系统 Linux 安装 / 升级脚本
#   主服务器（C# .NET 8，自包含） + 对话网关（Python 3 + FastAPI）
#
# 用法：
#   sudo ./install.sh /tmp/AiApprovalServer-linux-x64.tar.gz /tmp/xiaocainu-linux.tar.gz
#   sudo ./install.sh            # 默认就是上面两个 /tmp 路径
#
# 幂等：重复执行即升级。会保留服务器上已改过的 appsettings.json 与 config/，
# 以及 /etc/ai-approval/ 下的 env 文件（密钥不会被重新生成）。
set -euo pipefail

CSHARP_TAR="${1:-/tmp/AiApprovalServer-linux-x64.tar.gz}"
PY_TAR="${2:-/tmp/xiaocainu-linux.tar.gz}"

APP_ROOT=/opt/ai-approval
APP_DIR=$APP_ROOT/app
DATA_DIR=/var/lib/ai-approval
CONF_DIR=/etc/ai-approval

GW_ROOT=/opt/xiaocainu
GW_DIR=$GW_ROOT/app
GW_DATA=/var/lib/xiaocainu

SVC_USER=ai-approval
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

log()  { printf '\033[32m[+]\033[0m %s\n' "$*"; }
warn() { printf '\033[33m[!]\033[0m %s\n' "$*"; }
die()  { printf '\033[31m[x]\033[0m %s\n' "$*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || die "请用 root（sudo）执行"
command -v tar >/dev/null || die "缺少 tar"
command -v openssl >/dev/null || die "缺少 openssl"

# ---------- 1. 运行账号 ----------
if ! id -u "$SVC_USER" >/dev/null 2>&1; then
  log "创建系统账号 $SVC_USER"
  useradd --system --user-group --home "$APP_ROOT" --shell /usr/sbin/nologin "$SVC_USER"
else
  log "账号 $SVC_USER 已存在，跳过"
fi

# ---------- 2. 目录 ----------
log "创建目录"
install -d -o "$SVC_USER" -g "$SVC_USER" -m 0755 "$APP_ROOT" "$GW_ROOT"
install -d -o "$SVC_USER" -g "$SVC_USER" -m 0700 "$DATA_DIR" "$GW_DATA"
install -d -o root -g "$SVC_USER" -m 0750 "$CONF_DIR"

# ---------- 3. 主服务器（C#）----------
[ -f "$CSHARP_TAR" ] || die "找不到发布包：$CSHARP_TAR（先在开发机执行 dotnet publish -r linux-x64）"
log "解包主服务器 → $APP_DIR"

STAGE="$(mktemp -d)"
trap 'rm -rf "$STAGE"' EXIT
tar -xzf "$CSHARP_TAR" -C "$STAGE"
SRC="$STAGE/AiApprovalServer-linux-x64"
[ -x "$SRC/AiApprovalServer" ] || chmod +x "$SRC/AiApprovalServer"

# 保留服务器上改过的配置：appsettings.json 与 config/secrets.local.json
if [ -f "$APP_DIR/appsettings.json" ]; then
  log "保留现有 appsettings.json（新包里的同名文件不会覆盖它）"
  rm -f "$SRC/appsettings.json"
fi
if [ -d "$APP_DIR/config" ]; then
  log "保留现有 config/（secrets.local.json 不会被覆盖）"
  rm -rf "$SRC/config"
  cp -a "$APP_DIR/config" "$SRC/config"
fi

rm -rf "$APP_ROOT/app.old"
if [ -d "$APP_DIR" ]; then mv "$APP_DIR" "$APP_ROOT/app.old"; fi
mv "$SRC" "$APP_DIR"
chmod +x "$APP_DIR/AiApprovalServer"
chown -R "$SVC_USER":"$SVC_USER" "$APP_ROOT"

# ---------- 4. 对话网关（Python）----------
if [ -f "$PY_TAR" ]; then
  log "解包对话网关 → $GW_DIR"
  # venv 里全是绝对路径的 shebang，先整体挪出去、解包后再原样放回（省一次重装依赖）
  OLD_VENV=""
  if [ -x "$GW_DIR/.venv/bin/python" ]; then
    OLD_VENV="$GW_ROOT/.venv.old"
    rm -rf "$OLD_VENV"
    mv "$GW_DIR/.venv" "$OLD_VENV"
  fi
  rm -rf "$GW_ROOT/app.new"
  install -d "$GW_ROOT/app.new"
  tar -xzf "$PY_TAR" -C "$GW_ROOT/app.new"
  rm -rf "$GW_DIR"
  mv "$GW_ROOT/app.new/xiaocainu" "$GW_DIR"
  rmdir "$GW_ROOT/app.new" 2>/dev/null || true
  if [ -n "$OLD_VENV" ] && [ -d "$OLD_VENV" ]; then
    mv "$OLD_VENV" "$GW_DIR/.venv"
  fi
  chown -R "$SVC_USER":"$SVC_USER" "$GW_DIR"

  PYVER="$(python3 -c 'import sys;print("%d.%d"%sys.version_info[:2])' 2>/dev/null || echo "0.0")"
  log "Python 版本：$PYVER"
  case "$PYVER" in
    3.10|3.11|3.12|3.13) ;;
    *) warn "建议 Python 3.10+（当前 $PYVER），fastapi/pydantic 的新版本可能装不上" ;;
  esac

  if [ ! -x "$GW_DIR/.venv/bin/python" ]; then
    log "创建虚拟环境并安装依赖（需要联网访问 PyPI）"
    python3 -m venv "$GW_DIR/.venv"
  fi
  "$GW_DIR/.venv/bin/python" -m pip install --upgrade pip >/dev/null
  PYTHONUTF8=1 "$GW_DIR/.venv/bin/python" -m pip install -r "$GW_DIR/requirements.txt"
  chown -R "$SVC_USER":"$SVC_USER" "$GW_DIR/.venv"
else
  warn "未找到 $PY_TAR，跳过网关（只装主服务器）"
fi

# ---------- 5. 环境变量文件（只在缺失时生成密钥）----------
gen_env() { # $1=目标文件 $2=模板
  local target="$1" tpl="$2"
  local jwt aes pepper
  jwt="$(openssl rand -base64 48)"
  aes="$(openssl rand -base64 32)"
  pepper="$(openssl rand -base64 24)"
  sed -e "s|^AISERVER_JWT_SECRET=.*|AISERVER_JWT_SECRET=${jwt}|" \
      -e "s|^AISERVER_AES_KEY=.*|AISERVER_AES_KEY=${aes}|" \
      -e "s|^AISERVER_PEPPER=.*|AISERVER_PEPPER=${pepper}|" \
      "$tpl" > "$target"
  chown root:"$SVC_USER" "$target"
  chmod 0640 "$target"
  log "已生成 $target（含新密钥，请连同数据目录一起备份）"
}

if [ ! -f "$CONF_DIR/ai-approval.env" ]; then
  gen_env "$CONF_DIR/ai-approval.env" "$SCRIPT_DIR/ai-approval.env.example"
  warn "请编辑 $CONF_DIR/ai-approval.env：填 AISERVER_TRUSTED_ORIGINS 与 AISERVER_AI_KEY"
else
  log "$CONF_DIR/ai-approval.env 已存在，保留（不会重新生成密钥）"
fi

if [ ! -f "$CONF_DIR/xiaocainu.env" ]; then
  install -o root -g "$SVC_USER" -m 0640 "$SCRIPT_DIR/xiaocainu.env.example" "$CONF_DIR/xiaocainu.env"
  warn "请编辑 $CONF_DIR/xiaocainu.env：填 DEEPSEEK_API_KEY 与 FEISHU_* 凭证"
else
  log "$CONF_DIR/xiaocainu.env 已存在，保留"
fi

# ---------- 6. systemd ----------
log "安装 systemd 单元"
install -m 0644 "$SCRIPT_DIR/ai-approval.service" /etc/systemd/system/ai-approval.service
if [ -d "$GW_DIR" ]; then
  install -m 0644 "$SCRIPT_DIR/xiaocainu.service" /etc/systemd/system/xiaocainu.service
fi
systemctl daemon-reload
systemctl enable ai-approval.service >/dev/null
if [ -d "$GW_DIR" ]; then
  systemctl enable xiaocainu.service >/dev/null
fi

systemctl restart ai-approval.service
if [ -d "$GW_DIR" ]; then
  sleep 2
  systemctl restart xiaocainu.service
fi

# ---------- 7. 自检 ----------
sleep 3
echo
log "健康检查"
if curl -fsS http://127.0.0.1:8899/api/public/health; then
  echo
else
  warn "主服务器健康检查失败，看日志：journalctl -u ai-approval -n 60 --no-pager"
fi
if [ -d "$GW_DIR" ]; then
  if curl -fsS http://127.0.0.1:8000/health; then echo; else
    warn "网关健康检查失败，看日志：journalctl -u xiaocainu -n 60 --no-pager"
  fi
fi

cat <<'EOF'

下一步：
  1) 编辑 /etc/ai-approval/ai-approval.env   → AISERVER_TRUSTED_ORIGINS=https://ops.你的域名
  2) 编辑 /etc/ai-approval/xiaocainu.env     → DEEPSEEK_API_KEY、FEISHU_*
  3) 装 nginx 配置并签发证书（见 deploy/linux/nginx-ai-approval.conf）
  4) systemctl restart ai-approval xiaocainu
  5) 立刻改掉演示账号口令（employee/finance/admin/root）与初始超管口令
EOF
