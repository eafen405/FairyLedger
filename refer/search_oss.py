#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""搜索 GitHub 上的开源进销存/WMS 项目，输出精简评估表"""
import json
import urllib.request
import urllib.parse

QUERIES = [
    "进销存 仓库",
    "进销存管理系统",
    "wms 仓库 库存",
    "inventory management sqlite",
    "stock management open source",
]

seen = {}

for q in QUERIES:
    url = "https://api.github.com/search/repositories?sort=stars&order=desc&per_page=12&q=" + urllib.parse.quote(q)
    req = urllib.request.Request(url, headers={"Accept": "application/vnd.github+json", "User-Agent": "fairy-research"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            d = json.load(r)
    except Exception as e:
        print(f"[{q}] ERROR: {e}")
        continue
    for item in d.get("items", []):
        full = item["full_name"]
        if full in seen:
            continue
        seen[full] = True
        desc = (item.get("description") or "").strip()
        lang = item.get("language") or "?"
        stars = item["stargazers_count"]
        pushed = (item.get("pushed_at") or "")[:10]
        arch = item.get("archived", False)
        print(f"{stars:>6}★ {full:<45} [{lang:<12}] 更新:{pushed} {'归档' if arch else '活跃'}")
        if desc:
            print(f"        {desc[:150]}")
