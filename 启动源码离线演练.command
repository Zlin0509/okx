#!/bin/bash
set -euo pipefail
cd -- "$(dirname -- "$0")"
export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:$PATH"
if ! command -v uv >/dev/null 2>&1; then
  echo '请先运行 安装源码依赖.command；普通用户可直接使用 DMG 中的 .app。'
  exit 1
fi
echo '正在启动离线演练；按 Ctrl+C 暂停并退出。地址将在下方显示。'
exec env -i HOME="$HOME" PATH="$PATH" LANG=en_US.UTF-8 PYTHON_DOTENV_DISABLED=1 \
  uv run --frozen --no-dev --python 3.14.3 --no-env-file packaging/macos/server.py "$@"
