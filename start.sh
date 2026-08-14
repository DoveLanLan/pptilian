#!/usr/bin/env bash
# =============================================================================
# 提链工具 一键启动脚本 (macOS / Linux)
#
# 适配网络：代理池里的落地 IP 无法直连，必须先经本地前置 SOCKS 代理中转。
#   默认前置代理: socks5://127.0.0.1:10808  （可用环境变量 FRONT_PROXY 覆盖）
#   链路: 本地 -> 前置代理(10808) -> 代理池落地IP -> 目标站
#
# 用法:
#   ./start.sh                 # 一键启动（自动建 venv / 装依赖 / 装 gost）
#   FRONT_PROXY=socks5://127.0.0.1:10809 ./start.sh   # 自定义前置代理
#   PORT=8080 ./start.sh       # 自定义服务端口
# =============================================================================
set -u
cd "$(dirname "$0")" || exit 1
PROJECT_DIR="$(pwd)"

echo "=============================================="
echo "提链工具服务"
echo "项目目录: $PROJECT_DIR"
echo "=============================================="
echo

# ---------------------------------------------------------------------------
# 1. Python 虚拟环境 + 依赖
# ---------------------------------------------------------------------------
PYTHON_BIN="${PYTHON_BIN:-python3}"
if [ ! -x ".venv/bin/python" ]; then
  echo "[1/4] 创建虚拟环境 .venv ..."
  "$PYTHON_BIN" -m venv .venv || { echo "venv 创建失败，请确认已安装 python3"; exit 1; }
fi
PY=".venv/bin/python"

echo "[1/4] 检查依赖 ..."
if ! "$PY" -c "import flask, requests, socks, curl_cffi, qrcode, PIL, fastapi, uvicorn, pydantic, blinker, httpx, loguru, playwright, pproxy" >/dev/null 2>&1; then
  echo "      安装缺失依赖 (requirements.txt) ..."
  .venv/bin/pip install --quiet --disable-pip-version-check -r requirements.txt \
    || { echo "依赖安装失败，请检查网络后重试"; exit 1; }
else
  echo "      依赖已就绪，跳过安装。"
fi

# ---------------------------------------------------------------------------
# 2. gost (落地代理 -> 本地 HTTP 桥接所必需)
# ---------------------------------------------------------------------------
mkdir -p bin
GOST_BIN="bin/gost"
GOST_VERSION="2.12.0"
install_gost() {
  local os_arch="$1" url
  url="https://github.com/ginuerzh/gost/releases/download/v${GOST_VERSION}/gost_${GOST_VERSION}_${os_arch}.tar.gz"
  echo "      下载 gost ${GOST_VERSION} (${os_arch}) ..."
  curl -L --connect-timeout 15 --max-time 300 -o /tmp/gost_dl.tar.gz "$url" || return 1
  local tmpdir
  tmpdir="$(mktemp -d)"
  tar -xzf /tmp/gost_dl.tar.gz -C "$tmpdir" || { rm -rf "$tmpdir"; return 1; }
  local f
  f="$(find "$tmpdir" -type f -name 'gost' -perm -u+x | head -n1)"
  [ -n "$f" ] || f="$(find "$tmpdir" -type f -name 'gost' | head -n1)"
  [ -n "$f" ] || { rm -rf "$tmpdir"; return 1; }
  cp "$f" "$GOST_BIN"
  chmod +x "$GOST_BIN"
  rm -rf "$tmpdir" /tmp/gost_dl.tar.gz
  echo "      已安装到 $PROJECT_DIR/$GOST_BIN"
}
if [ ! -x "$GOST_BIN" ]; then
  echo "[2/4] 安装 gost ..."
  case "$(uname -s)/$(uname -m)" in
    Darwin/arm64)   install_gost "darwin_arm64" ;;
    Darwin/x86_64)  install_gost "darwin_amd64" ;;
    Linux/aarch64)  install_gost "linux_arm64" ;;
    Linux/x86_64)   install_gost "linux_amd64" ;;
    *)
      echo "      无法识别平台，请手动安装 gost v2 并放入 $PROJECT_DIR/bin/gost"
      echo "      (需支持 -L/-F 参数，见 https://github.com/ginuerzh/gost/releases)"
      ;;
  esac
else
  echo "[2/4] gost 已存在，跳过安装。"
fi

# 让 app.py 的 shutil.which("gost") 能找到 bin/gost
export PATH="$PROJECT_DIR/bin:$PATH"
if ! command -v gost >/dev/null 2>&1; then
  echo
  echo "  ⚠ 警告: 未找到 gost 可执行文件。"
  echo "    - 若启用了前置代理(FRONT_PROXY)或勾选'使用 GOST'，提链会因缺少 gost 报错。"
  echo "    - 可手动下载并解压到 $PROJECT_DIR/bin/gost:"
  echo "      https://github.com/ginuerzh/gost/releases/tag/v${GOST_VERSION}"
  echo
fi

# ---------------------------------------------------------------------------
# 3. 前置代理配置 + 连通性检查
# ---------------------------------------------------------------------------
# 允许外部覆盖；默认 socks5://127.0.0.1:10808
FRONT_PROXY="${FRONT_PROXY:-socks5://127.0.0.1:10808}"
export FRONT_PROXY
echo "[3/4] 前置代理: $FRONT_PROXY"

FRONT_HOST="$(printf '%s' "$FRONT_PROXY" | sed -E 's#^[a-zA-Z0-9]+://([^:@/]+).*#\1#')"
FRONT_PORT="$(printf '%s' "$FRONT_PROXY" | sed -nE 's#^[a-zA-Z0-9]+://[^:]+:([0-9]+).*#\1#p')"
if [ -n "${FRONT_PORT:-}" ] && command -v nc >/dev/null 2>&1; then
  if nc -z -w 3 "127.0.0.1" "$FRONT_PORT" >/dev/null 2>&1; then
    echo "      127.0.0.1:$FRONT_PORT 可达 ✓"
  else
    echo "  ⚠ 警告: 前置代理 127.0.0.1:$FRONT_PORT 当前不可达，请确认前置代理已启动。"
  fi
fi

# ---------------------------------------------------------------------------
# 4. 自动选择空闲端口 + 提升并发连接数上限 + 启动服务
# ---------------------------------------------------------------------------
# macOS AirPlay Receiver 常占用 5000 端口，从 5000 起自动顺延到第一个空闲端口。
if [ -z "${PORT:-}" ]; then
  PORT="$("$PY" - <<'PY'
import socket
for p in range(5000, 5100):
    s = socket.socket()
    try:
        s.bind(("127.0.0.1", p))
        print(p)
        break
    except OSError:
        pass
    finally:
        s.close()
PY
)"
  PORT="${PORT:-5000}"
fi
ulimit -n 65535 >/dev/null 2>&1 || true
export PORT
echo "[4/4] 服务地址: http://127.0.0.1:$PORT"
echo "      请在页面代理池粘贴落地 IP，勾选'使用 GOST'后即可经前置代理提链。"
echo "      (保持本窗口运行，Ctrl+C 停止)"
echo
exec "$PY" app.py
