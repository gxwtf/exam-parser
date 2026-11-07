#!/bin/bash
# 化学图片版：夜间流水线（裁图 → 转 MD）
#
# 1) 等两个裁图任务（crop_batch_chem.py）跑完；
# 2) 把 一模 / 上期末 / 真题 的 docx 转成 MD（w2m + libreoffice，本地跑，不调大模型）；
# 3) 打印汇总，交给早上的验收步（构建 paper.json + 入库 + 拆组）。
#
# 用法：nohup bash run_chem_nightly.sh > /tmp/chem-nightly.log 2>&1 &
set -u
cd "$(dirname "$0")" || exit 1

log() { echo "[$(date '+%m-%d %H:%M:%S')] $*"; }

log "=== 开始：等裁图任务结束 ==="
while pgrep -f "crop_batch_chem.py" > /dev/null; do
  running=$(pgrep -fc "crop_batch_chem.py" || true)
  log "裁图进行中（${running} 个进程），60s 后再看"
  sleep 60
done
log "裁图任务已结束"

for t in 一模 上期末 真题; do
  log "=== 转 MD：${t} / 化学 ==="
  python3 batch_convert_docx_to_md.py -T "$t" -s 化学 2>&1 | tail -15
  log "完成 ${t}：md 目录 $(ls -d "output_docx_to_md/${t}/化学"/* 2>/dev/null | wc -l) 个"
done

log "=== 产物统计 ==="
for t in 一模 上期末 真题; do
  log "${t}: 裁图 $(ls "output_image_questions/${t}/化学" 2>/dev/null | wc -l) 卷 / md $(ls -d "output_docx_to_md/${t}/化学"/* 2>/dev/null | wc -l) 个目录"
done
log "=== 夜间流水线结束（下一步：构建 paper.json + 入库 + 拆组）==="
