#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把钉钉「我的待办」同步到 wbdash 的「今天任务」

为什么要本地跑：
    钉钉应用接口（/v1.0/todo/users/{unionId}/tasks）只能读到「通过本应用创建」的待办，
    员工在钉钉客户端里手建的、审批产生的待办一律读不到（0 条）。
    但钉钉允许「本人授权的 CLI」读取自己的待办 —— dws todo +get-my-tasks 能读到全量。
    服务器上没有 dws，所以由本机拉取后推给工作台。

用法：
    python3 sync_todo.py                 # 正常同步（未完成 + 已完成全量）
    python3 sync_todo.py --dry-run       # 只看会写什么，不提交
    python3 sync_todo.py --open-only     # 只同步未完成的（快）
    python3 sync_todo.py --detail-limit 30   # 为最近 N 条补拉详情（拿真实跳转链接）
    python3 sync_todo.py --me 张三     # 指定同步谁的待办（默认本机 dws 登录人）

去重键：钉钉任务ID（taskId），重复跑不会产生重复记录。
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

# 钉钉优先级 10/20/30/40 → 工作台 高/中/低
PRIORITY_MAP = {'40': '高', '30': '中', '20': '中', '10': '低'}
# 待办来源标记 → 归属项目
PROJECT_MAP = {
    'certify_todo': '钉钉审批',
    'certify': '钉钉审批',
}
TODO_LINK = 'https://todo.dingtalk.com/'


def api(path, body=None, token=None):
    url = HOST + path
    data = json.dumps(body, ensure_ascii=False).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method='POST' if data else 'GET')
    req.add_header('Content-Type', 'application/json')
    if token:
        req.add_header('Authorization', 'Bearer ' + token)
    with urllib.request.urlopen(req, timeout=120) as r:
        return json.loads(r.read().decode())


def dws(args, timeout=240):
    p = subprocess.run(['dws'] + args, capture_output=True, text=True, timeout=timeout)
    if p.returncode != 0:
        raise RuntimeError((p.stderr or p.stdout or '').strip()[:300])
    try:
        return json.loads(p.stdout)
    except Exception:
        raise RuntimeError('dws 返回不是 JSON：' + (p.stdout or '')[:200])


def ms_to_date(ms):
    try:
        ms = int(ms)
        if ms <= 0:
            return ''
    except Exception:
        return ''
    d = datetime.datetime.fromtimestamp(ms / 1000)
    return d.strftime('%Y-%m-%d')


def fetch_tasks(status, me):
    """status: 'false' 未完成 / 'true' 已完成 → 列表"""
    args = ['todo', '+get-my-tasks', '--all', '--role-types',
            'creator,executor,participant', '--format', 'json']
    if status:
        args += ['--status', status]
    d = dws(args)
    data = d.get('data') or {}
    return data.get('todos') or data.get('tasks') or []


def fetch_detail(task_id):
    """拉单条详情，拿跳转链接 / 发起人 / 负责人"""
    try:
        d = dws(['todo', '+get', '--task-id', str(task_id), '--format', 'json'])
        return d.get('data') or {}
    except Exception as e:
        print('  详情拉取失败 %s：%s' % (task_id, str(e)[:80]))
        return {}


def to_record(t, detail, me):
    tid = str(t.get('taskId') or '').strip()
    subject = str(t.get('subject') or '').strip()
    if not tid or not subject:
        return None
    det = detail or {}
    is_done = det.get('isDone')
    if is_done is None:
        # 列表里 finalStatusStage：0=未完成，2=已完成
        is_done = str(t.get('finalStatusStage')) == '2'
    due = det.get('dueTime') or t.get('dueTime')
    link = ((det.get('detailUrl') or {}).get('pcUrl')
            or (det.get('detailUrl') or {}).get('appUrl') or TODO_LINK)
    # 负责人：优先取执行人里除自己以外的（审批场景执行人就是自己）
    owner = ''
    for e in det.get('executorInfos') or []:
        if e.get('name'):
            owner = e['name']
            break
    if not owner:
        owner = me
    creator = ((det.get('creatorInfo') or {}).get('name') or '')
    src = str(det.get('bizTag') or det.get('source') or '').strip()
    project = PROJECT_MAP.get(src, '钉钉待办')
    if creator and creator != me:
        project = '%s · %s发起' % (project, creator)
    return {
        '任务名称': subject,
        '优先级': PRIORITY_MAP.get(str(t.get('priority') or det.get('priority') or '20'), '中'),
        '截止时间': ms_to_date(due),
        '负责人': owner,
        '状态': '已完成' if is_done else '待处理',
        '所属项目': project,
        '处理链接': link,
        '来源应用': '钉钉',
        '钉钉任务ID': tid,
    }


def main():
    global HOST, ME
    ap = argparse.ArgumentParser()
    ap.add_argument('--dry-run', action='store_true')
    ap.add_argument('--open-only', action='store_true', help='只同步未完成的待办')
    ap.add_argument('--detail-limit', type=int, default=30,
                    help='为最近 N 条待办补拉详情（拿真实跳转链接），0=不拉')
    ap.add_argument('--me', default=ME, help='同步谁的待办（默认本机 dws 登录人）')
    ap.add_argument('--host', default=HOST)
    ap.add_argument('--password', default=PASSWORD)
    a = ap.parse_args()

    HOST, ME = a.host, a.me
    if not a.password:
        print('缺少管理员密码：请设置环境变量 WBDASH_PASSWORD，或加 --password 参数')
        sys.exit(2)
    token = api('/auth/admin', {'password': a.password})['token']

    print('拉取钉钉待办（%s）…' % ME)
    open_list = fetch_tasks('false', ME)
    done_list = [] if a.open_only else fetch_tasks('true', ME)
    print('未完成 %d 条 / 已完成 %d 条' % (len(open_list), len(done_list)))

    all_items = open_list + done_list
    if not all_items:
        print('没有需要同步的待办')
        return 0

    # 未完成的全拉详情（数量少、最需要链接）；已完成按上限拉最近的
    detail_ids = [str(t.get('taskId')) for t in open_list[:a.detail_limit]]
    remain = max(0, a.detail_limit - len(detail_ids))
    if remain:
        detail_ids += [str(t.get('taskId')) for t in done_list[:remain]]
    details = {}
    for i, tid in enumerate(detail_ids, 1):
        if not tid:
            continue
        details[tid] = fetch_detail(tid)
        if i % 10 == 0:
            print('  已拉详情 %d/%d' % (i, len(detail_ids)))

    payload, skipped = [], 0
    for t in all_items:
        tid = str(t.get('taskId') or '')
        rec = to_record(t, details.get(tid), ME)
        if rec:
            payload.append(rec)
        else:
            skipped += 1

    open_n = sum(1 for p in payload if p['状态'] != '已完成')
    print('待写入 %d 条（未完成 %d / 已完成 %d%s）'
          % (len(payload), open_n, len(payload) - open_n,
             '，跳过 %d' % skipped if skipped else ''))
    for rec in payload[:10]:
        print('  -', rec['状态'], '|', rec['截止时间'] or '无截止', '|',
              rec['任务名称'][:36], '|', rec['所属项目'])
    if a.dry_run:
        print('dry-run：未提交')
        return 0

    stat = api('/api/todo/upsert', {'items': payload}, token=token)
    print('写入结果：', stat)
    return 0


if __name__ == '__main__':
    sys.exit(main())
