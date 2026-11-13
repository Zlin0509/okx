#!/bin/bash
set -euo pipefail
cd -- "$(dirname -- "$0")"
export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:$PATH"
if ! command -v uv >/dev/null 2>&1; then
  echo '源码方式需要 uv（已验证 0.11.6）。普通用户请直接使用 DMG 中的 .app，无需安装。'
  exit 1
fi
exec env -i HOME="$HOME" PATH="$PATH" LANG=en_US.UTF-8 PYTHON_DOTENV_DISABLED=1 \
  uv sync --frozen --no-dev --python 3.14.3
