# curl -X GET "https://ai.gxwtf.cn/v1/models" \
#   -H "Authorization: Bearer sk-D8OSHxbmnZsH3DPIAueOOzTTGnfNcV1nSgoXLTXyBqBAdUi4"

curl -X POST "https://ai.gxwtf.cn/v1/chat/completions" \
  -H "Authorization: Bearer sk-D8OSHxbmnZsH3DPIAueOOzTTGnfNcV1nSgoXLTXyBqBAdUi4" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "deepseek-v4.1-flash",
    "messages": [
      {
        "role": "user",
        "content": "hello"
      }
    ],
    "reasoning_effort": "high"
  }'