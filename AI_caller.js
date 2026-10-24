import fs from 'fs';

const prompt = `
找出下面试卷（或试卷片段）的默写考察的诗句，对于填空形式的题目，返回时需要带上题干中的诗句部分。

不同诗句用换行符隔开，不得输出额外信息，不需要在每行前添加序号。

若找不到默写，输出：“找不到默写。”

例（你不需要把这个例子加入实际回复中）：

\`\`\`

15. 在横线上填写作品原句。

① “白日依山尽，”
② 《劝学》中用青、冰比喻的名句是 “\\_，\\_，\\_；\\_，\\_，\\_”

15. 黄河入海流

你应返回：

白日依山尽，黄河入海流。
青，取之于蓝，而青于蓝；冰，水为之，而寒于水。

\`\`\`

---

` + fs.readFileSync('prompt.txt').toString();

const body = JSON.stringify({
    "model": "DeepSeek-V4-Flash-0731",
    "messages": [
        {
            "role": "user",
            "content": prompt
        }
    ],
    "reasoning_effort": "high"
})

const MAX_RETRIES = 3;
let retryCount = 0;

function sendRequest() {
    return fetch("https://ai.gxwtf.cn/v1/chat/completions", {
        method: "POST",
        headers: {
            "Content-Type": "application/json",
            "Authorization": "Bearer sk-D8OSHxbmnZsH3DPIAueOOzTTGnfNcV1nSgoXLTXyBqBAdUi4"
        },
        body
    })
    .then(res=>res.json())
    .then(res=>{
        const response = res.choices[0].message.content;
        console.log(response);
    })
    .catch(err=>{
        console.error(err);
        retryCount++;
        if (retryCount >= MAX_RETRIES) {
            console.error(`连续 ${MAX_RETRIES} 次失败，退出程序`);
            process.exit(1);
        }
        console.error(`第 ${retryCount} 次失败，正在重试...`);
        return sendRequest();
    });
}

sendRequest();