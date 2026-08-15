#!/bin/bash
# 测试 DeepSeek API - 传入 prompt 文件，实时流式输出
# 用法: ./test_deepseek.sh <prompt文件路径>
# 示例: ./test_deepseek.sh data/debug/last_prompt.txt

set -e

if [ $# -lt 1 ]; then
    echo "用法: $0 <prompt文件路径>"
    exit 1
fi

PROMPT_FILE="$1"

if [ ! -f "$PROMPT_FILE" ]; then
    echo "错误: 文件不存在: $PROMPT_FILE"
    exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
if [ -f "$SCRIPT_DIR/.env" ]; then
    export $(grep -v '^#' "$SCRIPT_DIR/.env" | grep -v '^$' | xargs)
fi

if [ -z "$OPENAI_API_KEY" ]; then
    echo "错误: OPENAI_API_KEY 未设置"
    exit 1
fi

MODEL="${OPENAI_MODEL:-deepseek-v4-flash}"
API_URL="${OPENAI_BASE_URL:-https://api.deepseek.com}/chat/completions"

echo "模型: $MODEL"
echo "Prompt: $PROMPT_FILE"
echo "================================================"

jq -n \
    --rawfile prompt "$PROMPT_FILE" \
    '{
        model: ($ENV.OPENAI_MODEL // "deepseek-v4-flash"),
        messages: [
            {role: "system", content: "You are an expert English exam parser. Return ONLY valid JSON, no markdown, no explanations."},
            {role: "user", content: $prompt}
        ],
        thinking: {type: "disabled"},
        reasoning_effort: "high",
        stream: true
    }' | curl -N -s "$API_URL" \
    -H "Content-Type: application/json" \
    -H "Authorization: Bearer $OPENAI_API_KEY" \
    -d @- | while IFS= read -r line; do
    if [[ "$line" == "data: "* ]]; then
        data="${line#data: }"
        if [ "$data" = "[DONE]" ]; then
            echo ""
            break
        fi
        # 提取 content delta
        delta=$(echo "$data" | jq -r '.choices[0].delta.content // empty' 2>/dev/null)
        if [ -n "$delta" ]; then
            printf "%s" "$delta"
        fi
    fi
done

echo ""
echo "================================================"
echo "完成"