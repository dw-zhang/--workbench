# -*- coding: utf-8 -*-
"""一次性导入：wiki_tree.json（离线快照）→ 线上资料表
用法：docker exec wbdash python /app/t_wiki_import.py
"""
import json
import os
import time

import db

SRC = os.environ.get('WB_WIKI_JSON', '/app/data/wiki_tree.json')
FILES = db.TABLES['files']
ROOT = '__root__'

data = json.load(open(SRC, encoding='utf-8'))
nodes = data.get('nodes') or []
print('快照 fetchedAt = %s，知识库 %d 个，节点 %d 个'
      % (data.get('fetchedAt'), len(data.get('spaces') or []), len(nodes)))

# 1) 存量钉盘记录的知识库字段加「钉钉/」前缀（与知识库行一致，卡片才能显示来源徽标）
rows = db.query(FILES, size=5000).get('results') or []
print('现有资料行:', len(rows))
fixed = 0
for r in rows:
    kb = str(r.get('知识库') or '')
    rid = r.get('record_id') or r.get('_id')
    if kb and '/' not in kb and rid:
        db.update_record(FILES, rid, {'知识库': ('钉钉/' + kb)[:120]})
        fixed += 1
print('知识库字段加前缀:', fixed)

# 2) 已有节点ID 索引
known = {}
for r in db.query(FILES, size=5000).get('results') or []:
    nid = str(r.get('节点ID') or '').strip()
    if nid:
        known[nid] = r.get('record_id') or r.get('_id')
print('已有节点ID行:', len(known))

# 3) 子项数统计
kids = {}
for n in nodes:
    kids[n.get('parentId') or ''] = kids.get(n.get('parentId') or '', 0) + 1

added = updated = 0
t0 = time.time()
for n in nodes:
    nid = str(n.get('nodeId') or '').strip()
    if not nid:
        continue
    name = str(n.get('name') or '（无标题）')[:200]
    props = {
        '文件名称': name,
        '类型': n.get('type') or '其他',
        '知识库': ('钉钉/' + str(n.get('space') or ''))[:120],
        '节点ID': nid,
        '父节点ID': str(n.get('parentId') or ROOT),
        '层级': int(n.get('depth') or 0),
        '排序': int(n.get('sort') or 0),
        '子项数': int(kids.get(nid, 0)),
        '来源应用': '钉钉',
        '更新人': '钉钉知识库',
    }
    if n.get('path'):
        props['层级路径'] = str(n['path'])[:200]
    if n.get('spaceUrl'):
        props['空间链接'] = str(n['spaceUrl'])[:300]
    if n.get('url'):
        props['链接'] = str(n['url'])[:500]
    if n.get('update'):
        props['更新时间'] = str(n['update'])
    if n.get('kind') != 'folder':
        props['文档UUID'] = nid     # wiki 节点ID 即 dentryUuid
    rid = known.get(nid)
    if rid:
        db.update_record(FILES, rid, props)
        updated += 1
    else:
        db.add_record(FILES, props, source='dingtalk', person_id=None)
        added += 1

total = db.query(FILES, size=1)
rows2 = db.query(FILES, size=5000).get('results') or []
print('导入完成：新增 %d / 更新 %d / 资料表现共 %d 行 / 耗时 %.0fs'
      % (added, updated, len(rows2), time.time() - t0))
