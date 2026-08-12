#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""第二次交叉验证读图：重点核对图号、单价数字"""
import base64, json, urllib.request

key = ''
for line in open('/opt/data/.env'):
    if line.startswith('QWEN_EMBEDDING_API_KEY='):
        key = line.strip().split('=', 1)[1].strip().strip('"').strip("'")
        break

with open('/opt/data/cache/images/img_486e589ed5d6.jpg', 'rb') as f:
    b64 = base64.b64encode(f.read()).decode()

payload = {
    'model': 'qwen-vl-max',
    'messages': [{'role': 'user', 'content': [
        {'type': 'text', 'text': '这是一张柴油机配件价格表截图。请再次逐行精读并输出为 CSV 格式（序号,名称,图号,单价,合计），不要加其他说明文字。特别仔细核对每一行的图号数字（如 200-03-500 这类编号）和单价金额，看不清的图号用 [无] 标出，数字不确定用 [?] 标出。如果有表格标题或表头文字也请在第一行前用 # 注释输出。'},
        {'type': 'image_url', 'image_url': {'url': f'data:image/jpeg;base64,{b64}'}}
    ]}],
    'max_tokens': 3000
}

req = urllib.request.Request(
    'https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions',
    data=json.dumps(payload).encode(),
    headers={'Authorization': f'Bearer {key}', 'Content-Type': 'application/json'}
)
try:
    with urllib.request.urlopen(req, timeout=120) as r:
        d = json.load(r)
    if 'choices' in d:
        print(d['choices'][0]['message']['content'])
    else:
        print('ERROR:', json.dumps(d, ensure_ascii=False)[:600])
except Exception as e:
    print('EXC:', e)
