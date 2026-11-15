#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把钉钉「审批」同步到 wbdash 的审批表（补齐「我发起 / 抄送我」）

为什么要本地跑：
    服务端的应用接口只能读到「待我审批 / 我已处理」；
    「我发起 / 抄送我」需要钉钉 OA 高级版权限 Premium.Workflow.ReadWrite.All，
    当前应用没开通（403）。但本人授权的 dws 能直接读到这四类，所以由本机拉取后推送。

用法：
    python3 sync_oa.py                  # 正常同步（四类全量）
    python3 sync_oa.py --dry-run        # 只看会写什么，不提交
    python3 sync_oa.py --days 90        # 只同步最近 90 天发起的
    python3 sync_oa.py --no-detail      # 不拉详情（快，但状态只能按列表判断）

去重键：审批编号（processInstanceId），重复跑不会产生重复记录。
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
CORP_ID = os.environ.get('WBDASH_CORP_ID', '')
ME = os.environ.get('WBDASH_ME', '')
# 指定要同步谁的账号（dws 多 profile 支持：<corpId>:<userId>）
PROFILE = os.environ.get('WBDASH_DWS_PROFILE', '')
# 这些审批记录写到谁的名下（wbdash 的 person_id）；留空 = 管理员本人
PERSON_ID = os.environ.get('WBDASH_PERSON_ID', '')

# 审批类型关键字（与服务端 sync.py 的 TYPE_KEYWORDS 保持一致）
TYPE_KEYWORDS = [
    ('请假', ('请假', '调休', '年假', '病假', '事假')),
    ('报销', ('报销', '费用', '差旅费')),
    ('采购', ('采购', '购买', '申购')),
    ('用章', ('用章', '印章', '盖章')),
    ('出差', ('出差', '外出')),
    ('离职', ('离职', '交接')),
    ('绩效', ('绩效', 'KPI', '目标确认')),
    ('加班', ('加班',)),
    ('IT需求', ('ERP', 'IT', '需求', '系统', '权限')),
    ('发文', ('发文', '公告', '通知')),
]
SKIP_WORDS = ('邀请你试用', '邀请您试用', '邀请你体验', '邀请您体验',
              '邀请你加入', '邀请您加入', '开通提醒')

# 分类优先级：同一张单子出现在多类时，取靠前的标签
CATS = [
    ('待我审批', 'list-pending', True),
    ('我发起', 'my-initiated', False),
    ('抄送我', 'list-cc', False),
    ('我已处理', 'list-executed', False),
]
CAT_RANK = {'待我审批': 0, '我发起': 1, '抄送我': 2, '我已处理': 3}


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
    cmd = ['dws']
    if PROFILE:
        cmd += ['--profile', PROFILE]
    p = subprocess.run(cmd + list(args), capture_output=True, text=True, timeout=timeout)
    if p.returncode != 0:
        raise RuntimeError((p.stderr or p.stdout or '').strip()[:250])
    try:
        return json.loads(p.stdout)
    except Exception:
        raise RuntimeError('dws 返回不是 JSON：' + (p.stdout or '')[:200])


def type_of(title):
    t = str(title or '')
    for name, kws in TYPE_KEYWORDS:
        for k in kws:
            if k in t:
                return name
    return '其他'


def skip(title):
    t = str(title or '')
    return any(w in t for w in SKIP_WORDS)


def iso(ms):
    try:
        ms = int(ms)
        if ms <= 0:
            return ''
    except Exception:
        return ''
    d = datetime.datetime.fromtimestamp(ms / 1000)
    return d.strftime('%Y-%m-%dT%H:%M:%S+08:00')


def link(pid):
    return ('https://applink.dingtalk.com/approval/detail?corpId=%s&instanceId=%s&from=workbench'
            % (CORP_ID, pid))


def fetch_cat(cmd, start_ms, end_ms, max_pages=10):
    """按分类翻页拉取（单页上限 20）"""
    out = []
    for page in range(1, max_pages + 1):
        args = ['oa', '+' + cmd, '--page', str(page), '--limit', '20', '--format', 'json']
        if cmd == 'list-pending':
            args += ['--start', str(start_ms), '--end', str(end_ms)]
        d = dws(args)
        if not d.get('ok'):
            raise RuntimeError(str((d.get('error') or {}).get('message'))[:200])
        data = d.get('data') or {}
        # 各命令返回字段名不一致：list-* 用 instances，my-initiated 用 initiated
        ins = data.get('instances') or data.get('initiated') or data.get('list') or []
        out.extend(ins)
        if not ins or data.get('complete'):
            break
    return out


def fetch_detail(pid):
    """详情：拿准确状态（COMPLETED + processInstanceResult）"""
    for attempt in ('oa',):
        try:
            d = dws(['oa', 'approval', 'detail', '--instance-id', str(pid), '--format', 'json'])
            if d.get('errcode') not in (None, 0):
                return {}
            return d.get('result') or d.get('data') or {}
        except Exception:
            return {}
    return {}


def status_of(item, det):
    raw = str(det.get('status') or item.get('status') or '').upper()
    if raw in ('RUNNING', 'NEW', 'PROCESSING', ''):
        return '审批中'
    res = str(det.get('processInstanceResult') or '').lower()
    if res in ('refuse', 'refused', 'reject'):
        return '已拒绝'
    if raw in ('TERMINATED', 'CANCELED', 'CANCELLED', 'REVOKED'):
        return '已撤销'
    return '已通过'


def main():
    global HOST, ME, PROFILE, PERSON_ID
    ap = argparse.ArgumentParser()
    ap.add_argument('--dry-run', action='store_true')
    ap.add_argument('--days', type=int, default=180, help='同步最近 N 天发起的审批')
    ap.add_argument('--no-detail', action='store_true', help='不拉详情（快，状态按列表判断）')
    ap.add_argument('--detail-limit', type=int, default=80, help='最多为多少条拉详情')
    ap.add_argument('--me', default=ME, help='发起人姓名（记录里缺发起人时的兜底）')
    ap.add_argument('--profile', default=PROFILE,
                    help='dws 账号 profile，格式 <corpId>:<userId>；不填=当前登录账号')
    ap.add_argument('--person-id', default=PERSON_ID,
                    help='把审批写到哪个人的名下（wbdash person_id）；不填=管理员本人')
    ap.add_argument('--host', default=HOST)
    ap.add_argument('--password', default=PASSWORD)
    a = ap.parse_args()

    HOST, ME, PROFILE, PERSON_ID = a.host, a.me, a.profile, a.person_id
    if PROFILE:
        print('使用 dws 账号 profile：%s' % PROFILE)
    if not a.password:
        print('缺少管理员密码：请设置环境变量 WBDASH_PASSWORD，或加 --password 参数')
        sys.exit(2)
    token = api('/auth/admin', {'password': a.password})['token']

    now = int(datetime.datetime.now().timestamp() * 1000)
    start = now - a.days * 86400 * 1000

    # 同一实例取优先级最高的来源标签
    merged = {}
    for src, cmd, need_range in CATS:
        try:
            items = fetch_cat(cmd, start, now)
        except Exception as e:
            print('  [%s] 拉取失败：%s' % (src, str(e)[:120]))
            continue
        print('  %s：%d 条' % (src, len(items)))
        for it in items:
            pid = str(it.get('processInstanceId') or '').strip()
            if not pid:
                continue
            old = merged.get(pid)
            if not old or CAT_RANK[src] < CAT_RANK[old[0]]:
                merged[pid] = (src, it)
            elif old[0] == src:
                # 同分类更新为更新的状态
                merged[pid] = (src, it)

    if not merged:
        print('没有需要同步的审批')
        return 0
    print('去重后 %d 条' % len(merged))

    detail_n = 0
    payload, skipped = [], 0
    for pid, (src, it) in merged.items():
        title = str(it.get('title') or it.get('processInstanceTitle') or '').strip()
        det = {}
        if not a.no_detail and detail_n < a.detail_limit:
            det = fetch_detail(pid)
            detail_n += 1
            if not title:
                title = str(det.get('processInstanceTitle') or '').strip()
        if not title:
            skipped += 1
            continue
        if skip(title):
            skipped += 1
            continue
        origin = (str(it.get('originatorName') or '').strip()
                  or str(det.get('originatorUserid') or ''))
        # dws 会给离职员工加后缀「(已离职)」，界面上只留姓名
        origin = origin.replace('(已离职)', '').replace('（已离职）', '').strip()
        payload.append({
            '审批标题': title,
            '审批类型': type_of(title),
            '发起人': origin,
            '审批状态': status_of(it, det),
            '发起时间': iso(it.get('createTime') or det.get('createTime')),
            '审批编号': pid,
            '审批来源': src,
            '来源应用': '钉钉',
            '处理链接': link(pid),
        })

    from collections import Counter
    cnt = Counter(p['审批来源'] for p in payload)
    print('待写入 %d 条：%s%s' % (len(payload), dict(cnt),
                              '，跳过 %d' % skipped if skipped else ''))
    for rec in payload[:8]:
        print('  -', rec['审批来源'], '|', rec['审批状态'], '|',
              rec['发起时间'][:10], '|', rec['审批标题'][:36])
    if a.dry_run:
        print('dry-run：未提交')
        return 0

    body = {'items': payload}
    if PERSON_ID:
        body['person_id'] = PERSON_ID
        print('写入归属人：%s' % PERSON_ID)
    stat = api('/api/ding/upsert', body, token=token)
    print('写入结果：', stat)
    return 0


if __name__ == '__main__':
    sys.exit(main())
