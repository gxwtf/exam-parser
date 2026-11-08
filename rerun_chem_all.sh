#!/bin/bash
# 化学：全量重裁 → 重建 paper.json → 重导入（修题号识别后的一次性返工）
#
# 背景：crop_questions_pdf.py 的题号识别修了两处（题号必须连续；题号后可跟数字），
# 早批次裁出来的图里，`1．20223` 这种行整条被丢、题干里的「36、CO…」被当成题号，
# 结果不少卷少了第 1 题或大题。这里按新规则全量重裁并重导入。
#
# 用法：nohup bash rerun_chem_all.sh > /tmp/chem-rerun.log 2>&1 &
set -u
cd "$(dirname "$0")" || exit 1
log() { echo "[$(date '+%m-%d %H:%M:%S')] $*"; }

for t in 上期中 一模 上期末 二模 真题; do
  log "=== 重裁 ${t} / 化学 ==="
  python3 crop_batch_chem.py --type "$t" --year-from 2011 --must "" --force 2>&1 | tail -6
done

log "=== 重建 paper.json ==="
python3 build_papers_chem.py --type 上期中 --type 一模 --type 上期末 --year-from 2011 2>&1 | grep -E "完成：|只抽到" | tail -40
python3 build_papers_chem.py --type 二模 --year-from 2011 2>&1 | grep -E "完成：|只抽到" | tail -40
python3 build_papers_chem.py --type 真题 --year-from 2011 --must 北京 2>&1 | grep -E "完成：|只抽到" | tail -20

log "=== 重导入（--force）==="
cd /home/gxwtf/gxexercise || exit 1
for t in 上期中 一模 上期末 二模 真题; do
  log "--- 导入 $t"
  npx tsx scripts/import-image-questions.ts --all "/home/gxwtf/jingshibang/output_image_questions/$t/化学" --force 2>&1 | tail -3
done

log "=== 汇总 ==="
python3 - <<'PY'
import os, subprocess
env = dict(os.environ, PGPASSWORD="wddxgy_2714")
q = '''select count(*) || ' 套卷 / ' ||
 (select count(*) from "Question" where subject='化学') || ' 题 / ' ||
 (select count(*) from "QuestionGroup" where subject='化学') || ' 组' from "TestPaper" where subject='化学';'''
print(subprocess.run(["psql","-h","localhost","-U","postgres","-d","gxexercise","-t","-A","-c",q],
      capture_output=True, text=True, env=env).stdout.strip())
PY
log "=== 返工完成 ==="
