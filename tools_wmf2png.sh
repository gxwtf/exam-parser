#!/bin/bash
# 把 Word 公式图（.wmf）批量转成 PNG —— 机器上没有 libwmf 时用「免 root」方案：
#   apt-get download libwmf-bin libwmf-0.2-7 && dpkg-deb -x *.deb ex/
# 本脚本假定已解压到 /tmp/wmfbin/ex
set -euo pipefail
EX=${1:-/tmp/wmfbin/ex}
DIRS=${2:-/home/gxwtf/jingshibang/output_docx_to_md}
export LD_LIBRARY_PATH="$EX/usr/lib/x86_64-linux-gnu"
find "$DIRS" -name '*.wmf' -print0 |
  xargs -0 -P 8 -I{} sh -c '"'"$EX"'/usr/bin/wmf2gd -t png --maxwidth=1600 -o "${1%.wmf}.png" "$1" >/dev/null 2>&1' _ {}
echo "转换完成"
