#!/usr/bin/env bash
# 本地冒烟：导入上级目录的导出文件，跑三条典型查询
set -euo pipefail
cd "$(dirname "$0")/.."
python -m qflow sync --dir ..
python -m qflow ask "去年11月扣款合计"
python -m qflow ask "2025年11月对账"
python -m qflow ask "数据概览"
