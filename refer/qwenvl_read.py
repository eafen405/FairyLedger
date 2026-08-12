#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""用阿里云百炼 qwen-vl 读图"""
import base64, json, os, urllib.request

# 读取 .env 里的 QWEN key
key = ''
for line in open('/opt/data/.env'):
    if line.startswith('QWEN_EMBEDDING_API_KEY='):
        key = line.strip().split('=', 1)[1].strip().strip('"').strip("'")
        break
print('key prefix:', key[:10] if key else 'EMPTY')

with open('/opt/data/cache/images/img_486e589ed5d6.jpg', 'rb') as f:
    b64 = base64.b64encode(f.read()).decode()

payload = {
    'model': 'qwen-vl-max',
    'messages': [{'role': 'user', 'content': [
        {'type': 'text', 'text': '请完整、详细地转录这张图片的内容。如果是销售/进货记录表、账本、表格、Excel截图等，请逐条列出所有列（表头）和每一行数据，包括日期、名称、型号、图号、数量、单价、金额、客户等字段。数字和名称务必准确，看不清的地方标[不清]。如果有合计行、小计行也照录。'},
        {'type': 'image_url', 'image_url': {'url': f'data:image/jpeg;base64,{b64}'}}
    ]}],
    'max_tokens': 4000
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
