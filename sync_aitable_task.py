#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把钉钉多维表格（AI 表格）里「指派给我」的任务同步到 wbdash 任务表

为什么在本地跑：AI 表格没有可用的服务端「按人查任务」接口，只有本人身份的
dws CLI 能读到自己可见的 Base / 记录，所以由本机拉取后推给工作台。

判定「@我 / 指派给我」：记录里人员字段（执行人 / 负责人 / 责任人 / 处理人…）
包含我的 userId。只认这些字段名，避免把「固定资产-姓名」「调研-人员」当成任务。

字段自动识别（按字段名，取不到就按类型兜底）：
  标题  任务描述 / 任务名称 / 标题 / 事项 / 名称
  状态  状态 / 任务状态（singleSelect）
  优先级 优先级 / 任务优先级（singleSelect）
  截止  预计完成日期 / 截止时间 / 截止日期 / 计划完成时间（date）
  链接  Base 的 url（https://alidocs.dingtalk.com/i/nodes/<baseId>）

去重键：钉钉任务ID = aitbl:<recordId>，重复跑不会产生重复记录。

用法：
    python3 sync_aitable_task.py              # 正常同步
    python3 sync_aitable_task.py --dry-run    # 只看会写什么，不提交
    python3 sync_aitable_task.py --open-only  # 只同步未完成的（不进历史已完成）
    python3 sync_aitable_task.py --me-uid <你的 userid>
"""
import argparse
import datetime
import json
import os
import subprocess
import sys
import urllib.request

HOST = os.environ.get('WBDASH_HOST', 'http://127.0.0.1:8787')
PASSWORD = os.environ.get('WBDASH_PASSWORD', '')
ME = os.environ.get('WBDASH_ME', '')
ME_UID = os.environ.get('WBDASH_ME_UID', '')

# 只认这些人员字段名：命中才说明「这条记录指派了人」
USER_HINTS = ('执行人', '负责人', '责任人', '处理人', '指派给', '任务执行人', '跟进人', '负责人（执行人）')
STATUS_NAMES = ('状态', '任务状态', '进展状态')
PRIORITY_NAMES = ('优先级', '任务优先级', '紧急程度')
TITLE_NAMES = ('任务描述', '任务名称', '标题', '事项', '名称', '任务')
DUE_NAMES = ('预计完成日期', '截止时间', '截止日期', '计划完成时间', '要求完成日期', '到期日')

# 多维表格状态 → 工作台状态（工作台：待处理 / 进行中 / 已完成）
STATUS_MAP = {
    '已完成': '已完成', '完成': '已完成', '已关闭': '已完成', 'done': '已完成',
    '进行中': '进行中', '处理中': '进行中', 'doing': '进行中', '进行': '进行中',
    '未开始': '待处理', '待处理': '待处理', '待启动': '待处理', '待办': '待处理', 'todo': '待处理',
}
# 多维表格优先级 → 工作台优先级（高 / 中 / 低）
PRIORITY_MAP = {
    '重要紧急': '高', '紧急重要': '高', '最高': '高', '高': '高', 'P0': '高', 'P1': '高',
    '紧急不重要': '中', '重要不紧急': '中', '中': '中', '普通': '中', 'P2': '中',
    '不重要不紧急': '低', '低': '低', 'P3': '低',
}


def api(path, body=None, token=None):
    url = HOST + path
    data = json.dumps(body, ensure_ascii=False).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method='POST' if data else 'GET')
    req.add_header('Content-Type', 'application/json')
    if token:
        req.add_header('Authorization', 'Bearer ' + token)
    with urllib.request.urlopen(req, timeout=120) as r:
        return json.loads(r.read().decode())


def dws(args, timeout=180):
    p = subprocess.run(['dws'] + args, capture_output=True, text=True, timeout=timeout)
    if p.returncode != 0:
        raise RuntimeError((p.stderr or p.stdout or '').strip()[:300])
    raw = (p.stdout or '').strip()
    i = raw.find('{')
    if i < 0:
        raise RuntimeError('dws 返回不是 JSON：' + raw[:200])
    return json.loads(raw[i:])


def pick_field(fields, names, ftype=None):
    """按名字优先级挑字段，允许类型限制"""
    for n in names:
        for f in fields:
            if str(f.get('fieldName') or '') == n and (not ftype or f.get('type') == ftype):
                return f
    if ftype:
        for f in fields:
            if f.get('type') == ftype:
                return f
    return None


def cell_text(v):
    """把单元格值拍平成可读文本"""
    if v is None:
        return ''
    if isinstance(v, str):
        return v.strip()
    if isinstance(v, (int, float)):
        return str(v)
    if isinstance(v, dict):
        for k in ('name', 'text', 'value', 'title'):
            if v.get(k) not in (None, ''):
                return cell_text(v[k])
        return ''
    if isinstance(v, list):
        parts = [cell_text(x) for x in v]
        return '、'.join(p for p in parts if p)
    return str(v)


def cell_date(v):
    s = cell_text(v)
    return s[:10] if len(s) >= 10 and s[4] == '-' else ''


def cell_users(v):
    out = []
    for x in (v or []) if isinstance(v, list) else [v]:
        if isinstance(x, dict):
            out.append({'uid': str(x.get('userId') or x.get('userid') or ''),
                        'name': str(x.get('name') or x.get('userName') or '')})
        elif isinstance(x, str):
            out.append({'uid': '', 'name': x})
    return out


def scan_base(bid, bname, burl):
    """扫一个 Base，返回 (我的任务记录列表, 明细统计)"""
    got, stat = [], {'base': bname, 'tables': []}
    try:
        d = dws(['aitable', 'table', 'get', '--base-id', bid, '--format', 'json'])
    except Exception as e:
        print('  表列表失败（%s）：%s' % (bname, str(e)[:80]))
        return got, stat
    tables = (d.get('data') or {}).get('tables') or []
    for t in tables:
        tid, tname = t.get('tableId'), (t.get('tableName') or '')
        try:
            fd = dws(['aitable', 'field', 'get', '--base-id', bid, '--table-id', tid,
                      '--format', 'json'])
        except Exception:
            continue
        fdata = fd.get('data') or {}
        fields = fdata.get('fields') or fdata.get('items') or []
        if not fields:
            continue
        f_user = pick_field(fields, USER_HINTS)
        if not f_user:
            continue      # 没有「执行人/负责人」类字段 → 不是任务表，跳过
        f_status = pick_field(fields, STATUS_NAMES, 'singleSelect')
        f_pri = pick_field(fields, PRIORITY_NAMES, 'singleSelect')
        f_title = pick_field(fields, TITLE_NAMES, 'text') or pick_field(fields, TITLE_NAMES)
        f_due = pick_field(fields, DUE_NAMES, 'date')

        recs, cursor = [], None
        for _ in range(60):
            args = ['aitable', 'record', 'query', '--base-id', bid, '--table-id', tid,
                    '--limit', '100', '--format', 'json']
            if cursor:
                args += ['--cursor', cursor]
            try:
                q = dws(args)
            except Exception as e:
                print('  记录读取失败（%s/%s）：%s' % (bname, tname, str(e)[:80]))
                break
            recs += (q.get('data') or {}).get('records') or []
            cursor = (q.get('data') or {}).get('nextCursor')
            if not cursor:
                break

        mine = 0
        for r in recs:
            cells = r.get('cells') or r.get('fields') or {}
            users = cell_users(cells.get(f_user.get('fieldId')))
            if not any(u['uid'] == ME_UID for u in users):
                continue
            mine += 1
            title = cell_text(cells.get(f_title.get('fieldId'))) if f_title else ''
            if not title:
                title = '%s/%s 的任务' % (bname, tname)
            raw_status = cell_text(cells.get(f_status.get('fieldId'))) if f_status else ''
            status = STATUS_MAP.get(raw_status, raw_status or '待处理')
            raw_pri = cell_text(cells.get(f_pri.get('fieldId'))) if f_pri else ''
            due = cell_date(cells.get(f_due.get('fieldId'))) if f_due else ''
            owners = [u['name'] for u in users if u['name']]
            got.append({
                '任务名称': ' '.join(title.split())[:120],
                '优先级': PRIORITY_MAP.get(raw_pri, '中'),
                '截止时间': due,
                '负责人': '、'.join(owners)[:40] or ME,
                '状态': status,
                '所属项目': ('多维表格 · %s/%s' % (bname, tname))[:40],
                '处理链接': burl,
                '来源应用': '钉钉多维表格',
                '钉钉任务ID': 'aitbl:%s' % (r.get('recordId') or ''),
                '_raw_status': raw_status,
                '_raw_priority': raw_pri,
                '_table': '%s/%s' % (bname, tname),
            })
        stat['tables'].append({'table': '%s/%s' % (bname, tname), 'records': len(recs),
                               'mine': mine})
        if mine:
            print('  %s/%s：记录 %d 条，指派给我 %d 条' % (bname, tname, len(recs), mine))
    return got, stat


def main():
    global HOST, ME_UID
    ap = argparse.ArgumentParser()
    ap.add_argument('--dry-run', action='store_true')
    ap.add_argument('--open-only', action='store_true', help='只同步未完成的任务（不带历史已完成）')
    ap.add_argument('--me-uid', default=ME_UID, help='我的钉钉 userId（默认取 WBDASH_ME）')
    ap.add_argument('--host', default=HOST)
    ap.add_argument('--password', default=PASSWORD)
    a = ap.parse_args()

    HOST, ME_UID = a.host, a.me_uid
    if not a.password:
        print('缺少管理员密码：请设置环境变量 WBDASH_PASSWORD，或加 --password 参数')
        sys.exit(2)
    token = api('/auth/admin', {'password': a.password})['token']

    try:
        gl = dws(['aitable', 'base', 'list', '--format', 'json'])
    except Exception as e:
        print('AI 表格 Base 列表失败：%s' % str(e)[:160])
        return 1
    bases = (gl.get('data') or {}).get('bases') or []
    print('扫描 %d 个 AI 表格 Base…' % len(bases))

    tasks = []
    for b in bases:
        bid, bname = b.get('baseId'), (b.get('baseName') or '')
        burl = b.get('url') or ''
        if not burl:
            try:
                bd = dws(['aitable', 'base', 'get', '--base-id', bid, '--format', 'json'])
                burl = (bd.get('data') or {}).get('url') or ''
            except Exception:
                burl = ''
        if not burl:
            burl = 'https://alidocs.dingtalk.com/i/nodes/%s' % bid
        got, _stat = scan_base(bid, bname, burl)
        tasks += got

    if not tasks:
        print('没有指派给我的多维表格任务')
        return 0
    if a.open_only:
        tasks = [t for t in tasks if t['状态'] != '已完成']

    # 统计
    from collections import Counter
    st = Counter(t['状态'] for t in tasks)
    pr = Counter(t['优先级'] for t in tasks)
    today = datetime.date.today().isoformat()
    overdue = [t for t in tasks if t['状态'] != '已完成' and t['截止时间'] and t['截止时间'] < today]
    print()
    print('指派给我的任务 %d 条' % len(tasks))
    print('  状态：' + ' · '.join('%s %d' % (k, v) for k, v in st.most_common()))
    print('  优先级：' + ' · '.join('%s %d' % (k, v) for k, v in pr.most_common()))
    print('  逾期未完成：%d 条' % len(overdue))
    for t in overdue[:10]:
        print('    ! 逾期 %s | %s | %s' % (t['截止时间'], t['任务名称'][:36], t['_table']))
    print('  最近更新 / 进行中：')
    for t in [x for x in tasks if x['状态'] != '已完成'][:10]:
        print('    -', t['状态'], '|', t['截止时间'] or '无截止', '|',
              t['任务名称'][:40], '|', t['_table'])

    payload = []
    for t in tasks:
        payload.append({k: v for k, v in t.items() if not k.startswith('_')})

    if a.dry_run:
        print('dry-run：未提交（待写 %d 条）' % len(payload))
        return 0

    stat = api('/api/todo/upsert', {'items': payload}, token=token)
    print('写入结果：', stat)

    # 回读核对
    try:
        rows = api('/api/query?db=task&size=5000', token=token).get('results') or []
        at = [r for r in rows if str(r.get('来源应用') or '') == '钉钉多维表格']
        at_open = [r for r in at if str(r.get('状态') or '') != '已完成']
        print('回读核对：任务表共 %d 条，其中多维表格 %d 条（未完成 %d 条）'
              % (len(rows), len(at), len(at_open)))
    except Exception as e:
        print('回读核对失败：%s' % str(e)[:100])
    return 0


if __name__ == '__main__':
    sys.exit(main())
