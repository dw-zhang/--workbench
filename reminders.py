# -*- coding: utf-8 -*-
"""未完成任务 · 每日提醒

设计要点
- 每天只在设定时间之后发一次（配置 remind:last 记日期，重启不会重复发）
- 按人聚合：一个人一条钉钉工作通知，列出他名下未完成任务（逾期优先），不逐条刷屏
- 已完成（状态=已完成）不计入；可选「只提醒已逾期」
- 只推给有钉钉账号的人；没账号的跳过并计数
- 触发入口两个：调度器每 30 秒 tick() + 管理员手动 /api/remind/run
"""
import datetime
import json

import db
import dingtalk
import settings

CAP_DEFAULT = 8


class RemindError(Exception):
    pass


# ── 配置 ──────────────────────────────────────────────────────────────
def cfg():
    return {
        'enabled': str(settings.g('remind:daily', '0')) == '1',
        'time': (settings.g('remind:time', '09:00') or '09:00')[:5],
        'overdue_only': str(settings.g('remind:overdue_only', '0')) == '1',
        'cap': int(settings.g('remind:cap', str(CAP_DEFAULT)) or CAP_DEFAULT),
        'last': settings.g('remind:last', '') or '',
        'last_stat': settings.g('remind:last_stat', '') or '',
    }


def save(enabled=None, time_=None, overdue_only=None, cap=None):
    upd = {}
    if enabled is not None:
        upd['remind:daily'] = '1' if enabled else '0'
    if time_ is not None:
        t = str(time_).strip()[:5]
        if len(t) != 5 or t[2] != ':':
            raise RemindError('提醒时间格式应为 HH:MM')
        upd['remind:time'] = t
    if overdue_only is not None:
        upd['remind:overdue_only'] = '1' if overdue_only else '0'
    if cap is not None:
        upd['remind:cap'] = str(max(1, min(30, int(cap))))
    if upd:
        db.cfg_set(upd)
    return cfg()


# ── 取数 ──────────────────────────────────────────────────────────────
def pending_by_person(overdue_only=False):
    """未完成任务按人聚合 {person_id: [item...]}
    同一任务若有多个执行人（多条记录），各自只看到自己那条，不会重复。
    item = {title, due, project, overdue_days, link, priority}"""
    today = datetime.date.today()
    c = db.connect()
    with db._lock:
        rows = c.execute("SELECT id, data, person_id FROM records "
                         "WHERE database='task' ORDER BY seq DESC").fetchall()
    out = {}
    for r in rows:
        per = (r['person_id'] or '').strip()
        if not per:
            continue
        try:
            d = json.loads(r['data'] or '{}')
        except Exception:
            continue
        if str(d.get('状态') or '').strip() == '已完成':
            continue
        due = str(d.get('截止时间') or '')[:10]
        od = 0
        if due:
            try:
                od = (today - datetime.date(*[int(x) for x in due.split('-')])).days
                if od < 0:
                    od = 0
            except Exception:
                od = 0
        if overdue_only and not (due and due < today.isoformat()):
            continue
        out.setdefault(per, []).append({
            'record_id': r['id'],
            'title': str(d.get('任务名称') or '').strip()[:60] or '（未命名任务）',
            'due': due, 'overdue_days': od,
            'project': str(d.get('所属项目') or '').strip()[:20],
            'priority': str(d.get('优先级') or '').strip(),
            'link': str(d.get('处理链接') or '').strip(),
        })
    for items in out.values():
        # 逾期久的排前面，其次按截止日期
        items.sort(key=lambda x: (-x['overdue_days'], x['due'] or '9999-12-31'))
    return out


def done_today():
    """今天完成的任务数（按人）"""
    today = datetime.date.today().isoformat()
    c = db.connect()
    with db._lock:
        rows = c.execute("SELECT data, person_id FROM records WHERE database='task' "
                         "AND person_id IS NOT NULL").fetchall()
    out = {}
    for r in rows:
        try:
            d = json.loads(r['data'] or '{}')
        except Exception:
            continue
        if str(d.get('状态') or '') != '已完成':
            continue
        stamp = str(d.get('更新时间') or d.get('完成时间') or '')[:10]
        if stamp == today:
            out[r['person_id'] or ''] = out.get(r['person_id'] or '', 0) + 1
    return out


# ── 组装文案 ──────────────────────────────────────────────────────────
def _base_url():
    pc = settings.get_platform('dingtalk') or {}
    return (settings.g('site:public_url') or pc.get('public_url') or '').strip().rstrip('/')


def compose(items, cap=8, done=0):
    total = len(items)
    overdue = sum(1 for x in items if x['overdue_days'] > 0)
    head = '今日待办 %d 项' % total
    if overdue:
        head += '（逾期 %d）' % overdue
    if done:
        head += ' · 已完成 %d' % done
    lines = []
    for x in items[:cap]:
        seg = ['- %s' % x['title']]
        meta = []
        if x['project']:
            meta.append(x['project'])
        if x['overdue_days'] > 0:
            meta.append('逾期 %d 天' % x['overdue_days'])
        elif x['due']:
            meta.append('截止 %s' % x['due'][5:])
        if x['priority'] == '高':
            meta.append('高优')
        if meta:
            seg.append('（%s）' % ' · '.join(meta))
        lines.append(''.join(seg))
    if total > cap:
        lines.append('- … 还有 %d 项，点开工作台查看' % (total - cap))
    md = '\n'.join(lines)
    if overdue:
        md = '⚠️ 有 %d 项已逾期，请优先处理\n\n%s' % (overdue, md)
    return head, md


def _client():
    pc = settings.get_platform('dingtalk') or {}
    agent = str(pc.get('agent_id') or '')
    if not agent or not pc.get('app_key') or not pc.get('app_secret'):
        raise RemindError('钉钉应用未配置完整（app_key/app_secret/agent_id）')
    return dingtalk.Client(pc['app_key'], pc['app_secret'], pc.get('corp_id', '')), agent


# ── 发送 ──────────────────────────────────────────────────────────────
def run_daily(force=False, only_person=None, say=None):
    """执行一次每日提醒。force=True 忽略「今天已发」限制（手动测试用）"""
    c = cfg()
    today = datetime.date.today().isoformat()
    if not force and (c['last'] or '')[:10] == today:
        return {'ok': True, 'skipped': True, 'reason': '今天已提醒过',
                'sent': 0, 'total_person': 0}
    groups = pending_by_person(c['overdue_only'])
    if only_person:
        groups = {only_person: groups.get(only_person) or []}
    done = done_today()
    stat = {'ok': True, 'skipped': False, 'sent': 0, 'tasks': 0,
            'no_account': 0, 'failed': [], 'date': today}
    try:
        cli, agent = _client()
    except RemindError as e:
        stat.update({'ok': False, 'error': str(e)})
        return stat
    base = _base_url()
    for pid, items in groups.items():
        if not items:
            continue
        uids = [a.get('account_id') for a in db.accounts_of(pid, 'dingtalk') if a.get('account_id')]
        if not uids:
            stat['no_account'] += 1
            continue
        title, md = compose(items, c['cap'], done.get(pid, 0))
        try:
            r = cli.notify_work(agent, uids, title, md, url=base)
            code = r.get('errcode') if isinstance(r, dict) else 0
            if code not in (0, None):
                raise RemindError('errcode=%s %s' % (code, (r or {}).get('errmsg')))
            stat['sent'] += 1
            stat['tasks'] += len(items)
        except Exception as e:
            stat['failed'].append('%s: %s' % (pid, str(e)[:80]))
    if not only_person:
        db.cfg_set({'remind:last': today,
                    'remind:last_stat': json.dumps({k: stat[k] for k in
                                                   ('sent', 'tasks', 'date')},
                                                  ensure_ascii=False)})
    db.log('remind', not stat['failed'], stat['sent'],
           '每日待办提醒：%d 人 / %d 项%s' % (stat['sent'], stat['tasks'],
                                       ('，失败 %d' % len(stat['failed'])) if stat['failed'] else ''))
    if say:
        say('每日提醒已发：%d 人 / %d 项' % (stat['sent'], stat['tasks']))
    return stat


def tick(say=None):
    """调度器每 30 秒调用：到点且今天没发过 → 发一次"""
    try:
        c = cfg()
        if not c['enabled']:
            return None
        now = datetime.datetime.now()
        if now.strftime('%H:%M') < c['time']:
            return None
        if (c['last'] or '')[:10] == now.date().isoformat():
            return None
        return run_daily(say=say)
    except Exception as e:
        db.log('remind', False, 0, '每日提醒异常：%s' % str(e)[:150])
        return None


def preview(limit=5):
    """前端预览：今天会提醒谁、每人几条（不发消息）"""
    c = cfg()
    groups = pending_by_person(c['overdue_only'])
    out = []
    for pid, items in groups.items():
        nm = (db.person_get(pid) or {}).get('name') or ''
        if not nm:
            continue
        uids = [a.get('account_id') for a in db.accounts_of(pid, 'dingtalk') if a.get('account_id')]
        out.append({'name': nm, 'person_id': pid, 'count': len(items),
                    'overdue': sum(1 for x in items if x['overdue_days'] > 0),
                    'reachable': bool(uids),
                    'top': [x['title'] for x in items[:3]]})
    out.sort(key=lambda x: (-x['overdue'], -x['count']))
    return {'config': c, 'people': out[:limit], 'total_person': len(out),
            'total_task': sum(x['count'] for x in out)}
