#!/usr/bin/env bash
# ============================================================
#  工作台 一键安装 / 升级脚本
#  用法：
#    ./install.sh                 默认安装到当前目录，端口 8787
#    ./install.sh --port 9000     换端口
#    ./install.sh --seed          首次导入演示数据（看效果）
#    ./install.sh --yes           不交互（自动装 docker、自动确认）
#    ./install.sh --uninstall     停止并删除容器（数据保留在 ./data）
# ============================================================
set -euo pipefail
cd "$(dirname "$0")"

PORT=8787
SEED=0
ASSUME_YES=0
UNINSTALL=0

while [ $# -gt 0 ]; do
  case "$1" in
    --port) PORT="${2:?--port 需要数字}"; shift 2 ;;
    --port=*) PORT="${1#*=}"; shift ;;
    --seed) SEED=1; shift ;;
    --yes|-y) ASSUME_YES=1; shift ;;
    --uninstall) UNINSTALL=1; shift ;;
    -h|--help) sed -n '2,12p' "$0"; exit 0 ;;
    *) echo "未知参数：$1（用 --help 看用法）"; exit 1 ;;
  esac
done

say()  { printf '\033[36m==>\033[0m %s\n' "$*"; }
ok()   { printf '\033[32m✅\033[0m %s\n' "$*"; }
warn() { printf '\033[33m⚠️ \033[0m %s\n' "$*"; }
die()  { printf '\033[31m❌\033[0m %s\n' "$*" >&2; exit 1; }

# ---------- 1. 找 docker ----------
COMPOSE=""
detect_docker() {
  command -v docker >/dev/null 2>&1 || return 1
  if docker compose version >/dev/null 2>&1; then COMPOSE="docker compose"; return 0; fi
  if command -v docker-compose >/dev/null 2>&1; then COMPOSE="docker-compose"; return 0; fi
  return 1
}

install_docker() {
  warn "没检测到 docker（或 compose 插件），准备安装。"
  if [ "$ASSUME_YES" -ne 1 ]; then
    printf '是否现在用官方脚本安装 Docker？[y/N] '
    read -r a || true
    case "$a" in y|Y|yes|YES) ;; *) die "已取消。请先自行安装 Docker：https://docs.docker.com/engine/install/" ;; esac
  fi
  if command -v apt-get >/dev/null 2>&1; then
    sudo apt-get update -y && sudo apt-get install -y ca-certificates curl
  elif command -v dnf >/dev/null 2>&1; then
    sudo dnf install -y curl
  elif command -v yum >/dev/null 2>&1; then
    sudo yum install -y curl
  fi
  curl -fsSL https://get.docker.com | sudo sh
  sudo systemctl enable --now docker 2>/dev/null || true
  # 当前用户加入 docker 组，免 sudo（需重新登录才彻底生效，这里用 sudo 兜底）
  sudo usermod -aG docker "$(id -un)" 2>/dev/null || true
}

if ! detect_docker; then
  install_docker
  detect_docker || COMPOSE="sudo docker compose"
fi
# 当前用户无 docker 权限时自动加 sudo
if ! docker info >/dev/null 2>&1; then
  COMPOSE="sudo ${COMPOSE}"
  DOCKER="sudo docker"
else
  DOCKER="docker"
fi

# ---------- 2. 卸载 ----------
if [ "$UNINSTALL" -eq 1 ]; then
  say "停止并删除容器（./data 数据保留）"
  $COMPOSE down || true
  ok "已移除容器；如需彻底清空数据，手工删除 ./data 目录"
  exit 0
fi

# ---------- 3. 生成 .env ----------
if [ ! -f .env ]; then
  say "生成 .env（随机管理员密码）"
  PASS="$( (command -v openssl >/dev/null 2>&1 && openssl rand -base64 12) || python3 -c 'import secrets;print(secrets.token_urlsafe(12))' )"
  cat > .env <<EOF
# 由 install.sh 自动生成于 $(date '+%Y-%m-%d %H:%M:%S')
WB_ADMIN_PASS=${PASS}
WB_HOST_PORT=${PORT}
WB_NAME=workbench
TZ=Asia/Shanghai
WB_SEED=${SEED}
WB_DEV_LOGIN=0
EOF
  chmod 600 .env
  GENERATED_PASS="$PASS"
else
  say ".env 已存在，沿用其中配置"
  # 已有 .env 但端口参数覆盖时，同步端口
  if grep -q '^WB_HOST_PORT=' .env; then
    sed -i.bak "s/^WB_HOST_PORT=.*/WB_HOST_PORT=${PORT}/" .env && rm -f .env.bak
  else
    echo "WB_HOST_PORT=${PORT}" >> .env
  fi
  GENERATED_PASS="$(grep -E '^WB_ADMIN_PASS=' .env | cut -d= -f2- || true)"
fi

[ -d data ] || mkdir -p data

# ---------- 4. 构建并启动 ----------
say "构建镜像（首次约 10~60 秒）"
$COMPOSE build

say "启动容器"
$COMPOSE up -d

# ---------- 5. 等健康检查 ----------
say "等待服务就绪"
URL="http://127.0.0.1:${PORT}/api/ping"
READY=0
for i in $(seq 1 40); do
  if curl -fsS -m 3 "$URL" 2>/dev/null | grep -q '"ok"'; then READY=1; break; fi
  sleep 1.5
done
if [ "$READY" -eq 0 ]; then
  warn "健康检查超时，最近日志如下："
  $COMPOSE logs --tail=40 || true
  die "启动失败，请把上面的日志发出来"
fi

HOSTIP="$(hostname -I 2>/dev/null | awk '{print $1}')"
[ -n "${HOSTIP:-}" ] || HOSTIP="服务器IP"

cat <<EOF

────────────────────────────────────────────────
 ✅ 安装完成
────────────────────────────────────────────────
 访问地址   http://${HOSTIP}:${PORT}
 本机地址   http://127.0.0.1:${PORT}
 管理员密码 ${GENERATED_PASS:-（见 .env 里的 WB_ADMIN_PASS）}
 数据目录   $(pwd)/data     （备份就是拷走这个目录）
 容器管理   $COMPOSE ps / logs -f / restart / down

 接下来：
 1) 浏览器打开上面的地址 → 右上角「管理员登录」
 2) 控制台 → 平台配置 → 填钉钉 AppKey / AppSecret / CorpId → 保存
 3) 人员管理 → 拉取通讯录 → 回首页点「立即同步」
 4) 钉钉开放平台里把「应用首页地址 / 登录回调域名」填成上面的访问地址

 详细步骤见 docs/install.md，权限清单见 docs/permissions.md
────────────────────────────────────────────────
EOF
