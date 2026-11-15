#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""钉钉消息通知：统一队列 + 按人汇总

为什么要聚合：待办、等待回复、审批、我发起的审批结果、@我 分散在五条链路里，
各自发一条的话，一轮同步能给人刷出七八条消息。这里统一进一个队列，按人分组，
每人每轮只发一条摘要，点开直达对应分类。

事件结构：
    p  person_id
    k  种类：approval 待我审批 / cc 抄送我 / result 审批结果
           todo 新待办 / at 有人@我 / wait 等待回复
    t  标题
    u  链接
    s  事件毫秒时间
    x  补充说明（发起人 / 审批结果 / 发送人）

队列持久化在配置 notify:queue，重启不丢；超过 TTL（3 天）自动丢弃，避免某次
发送失败后越积越多。免打扰时段（默认 22:00-08:00）只攒不发。
"""
import datetime
import json
import threading
import time

import db
import settings
import dingtalk

QUEUE_KEY = 'notify:queue'
TTL_MS = 3 * 86400 * 1000          # 事件最长保留 3 天
MAXQ = 2000                        # 队列上限，超出丢最旧的
MAX_PER_FLUSH = 40                 # 单轮最多推送多少人的消息
BATCH_SAVE = 20                    # 每攒够多少条落一次盘

KINDS = {
    'approval': ('待我审批', 5),
    'result': ('审批结果', 5),
    'todo': ('新待办', 5),
    'cc': ('抄送我', 4),
    'at': ('有人@我', 4),
    'wait': ('等待回复', 4),
}
ORDER = ['approval', 'result', 'todo', 'cc', 'at', 'wait']

_lock = threading.RLock()
_send_lock = threading.Lock()
_cache = None
_dirty = 0
# 静默名单：某人第一次被同步时只建档不发消息，否则全公司一次性收到
# 「你有 N 条待审批」，等于上线当天给所有人发垃圾短信
_silent = set()


def silence(pid, on=True):
    if not pid:
        return
    with _lock:
        if on:
            _silent.add(pid)
        else:
            _silent.discard(pid)


def is_silent(pid):
    with _lock:
        return pid in _silent


# ── 队列读写 ──────────────────────────────────────────────
def _load():
    global _cache
    if _cache is None:
        try:
            raw = db.cfg_get(QUEUE_KEY) or '[]'
            got = json.loads(raw)
        except Exception:
            got = []
        _cache = got if isinstance(got, list) else []
    return _cache


def _save(items=None):
    global _cache, _dirty
    if items is not None:
        _cache = list(items)
    _dirty = 0
    try:
        db.cfg_set({QUEUE_KEY: json.dumps(_cache or [], ensure_ascii=False)})
    except Exception:
        pass


def pending():
    """队列里还有多少条没发（供状态页/调试用）"""
    with _lock:
        return len(_load())


def push(pid, kind, title, link='', ts=None, extra=''):
    """登记一条待发消息；同一人同类同标题在队列里去重"""
    global _dirty
    title = str(title or '').strip()
    if not pid or not title:
        return False
    if is_silent(pid):
        return False          # 首次建档的人：只入库，不打扰
    title = title[:80]
    extra = str(extra or '').strip()[:16]
    try:
        ms = int(ts) if ts else int(time.time() * 1000)
    except Exception:
        ms = int(time.time() * 1000)
    with _lock:
        q = _load()
        for e in reversed(q[-200:]):
            if e.get('p') == pid and e.get('k') == kind and e.get('t') == title:
                return False
        q.append({'p': pid, 'k': kind, 't': title, 'u': link or '',
                  's': ms, 'x': extra})
        if len(q) > MAXQ:
            del q[:len(q) - MAXQ]
        _dirty += 1
        _save()              # 逐条落盘：进程重启不丢，队列量级只有几十条
    return True


def push_many(pid, kind, rows):
    """rows = [(标题, 链接, 毫秒时间, 补充)]，批量登记"""
    n = 0
    for r in rows:
        try:
            t, u, ms, x = (list(r) + ['', None, ''])[:4]
        except Exception:
            continue
        if push(pid, kind, t, u, ms, x):
            n += 1
    return n


# ── 免打扰 ────────────────────────────────────────────────
def _in_quiet():
    raw = (settings.g('dingtalk:notify_quiet', '22:00-08:00') or '').strip()
    if not raw or '-' not in raw:
        return False
    try:
        a, b = raw.split('-', 1)
        sh, sm = [int(x) for x in a.strip().split(':')]
        eh, em = [int(x) for x in b.strip().split(':')]
    except Exception:
        return False
    now = datetime.datetime.now()
    cur = now.hour * 60 + now.minute
    start, end = sh * 60 + sm, eh * 60 + em
    if start == end:
        return False
    if start > end:                    # 跨零点，如 22:00-08:00
        return cur >= start or cur < end
    return start <= cur < end


# ── 组装文案 ──────────────────────────────────────────────
def _compose(evs, base, when=''):
    by = {}
    for e in evs:
        by.setdefault(str(e.get('k') or 'wait'), []).append(e)
    total = len(evs)
    blocks = []
    for k in ORDER:
        items = by.get(k) or []
        if not items:
            continue
        items.sort(key=lambda x: -(int(x.get('s') or 0)))
        label, cap = KINDS.get(k, ('提醒', 4))
        lines = []
        for e in items[:cap]:
            t = str(e.get('t') or '')[:42]
            x = str(e.get('x') or '')[:14]
            lines.append('- %s%s' % (t, ('（%s）' % x) if x else ''))
        if len(items) > cap:
            lines.append('- … 该分类共 %d 条' % len(items))
        blocks.append('**%s（%d）**\n%s' % (label, len(items), '\n'.join(lines)))

    if total == 1:
        e = evs[0]
        label = KINDS.get(str(e.get('k') or ''), ('提醒',))[0]
        title = '%s：%s' % (label, str(e.get('t') or '')[:36])
        url = str(e.get('u') or base or '')
    else:
        title = ('%s%d 项待处理' % ('早间摘要 · ' if when == 'morning' else '工作台提醒：', total))
        url = base or ''
    md = '\n\n'.join(blocks)
    if base and url and url != base:
        md += '\n\n[打开工作台](%s)' % base
    return title[:58], md, url


# ── 发送 ─────────────────────────────────────────────────
def flush(say=None, min_age=0):
    """把队列里的事件按人汇总发一条；min_age（秒）用于攒批，未满的留到下轮"""
    if settings.g('dingtalk:notify_digest', '1') == '0':
        return 0
    if not _send_lock.acquire(False):
        return 0            # 已有线程在发，这一轮让给它，避免重复推送
    try:
        return _flush_inner(say, min_age)
    finally:
        _send_lock.release()


def _flush_inner(say=None, min_age=0):
    with _lock:
        q = list(_load())
    if not q:
        return 0
    now_ms = int(time.time() * 1000)
    q = [e for e in q if now_ms - int(e.get('s') or 0) <= TTL_MS]
    if not q:
        _save([])
        return 0
    # 本轮真正要发的事件：过了攒批时间，且不在失败退避期内（rt 到点）
    go = [e for e in q if now_ms >= int(e.get('rt') or 0)]
    if min_age:
        floor = now_ms - int(min_age) * 1000
        go = [e for e in go if int(e.get('s') or 0) <= floor]
    if not go:
        _save(q)
        return 0
    if _in_quiet():
        _save(q)
        return 0

    pc = settings.get_platform('dingtalk') or {}
    agent = str(pc.get('agent_id') or '')
    if not agent or not pc.get('app_key'):
        return 0
    try:
        cli = dingtalk.Client(pc['app_key'], pc['app_secret'], pc.get('corp_id', ''))
    except Exception:
        return 0
    base = (settings.g('site:public_url') or pc.get('public_url') or '').strip().rstrip('/')

    # 本轮不处理的事件（攒批未到 / 退避期内）原样保留
    go_ids = set(id(e) for e in go)
    untouched = [e for e in q if id(e) not in go_ids]

    groups = {}
    for e in go:
        groups.setdefault(str(e.get('p') or ''), []).append(e)

    left, sent, fails, done, dropped = untouched, 0, [], set(), 0
    for pid, evs in groups.items():
        if sent >= MAX_PER_FLUSH:
            left.extend(evs)     # 本轮发送额度用完，原样保留到下轮
            continue
        uids = [a.get('account_id') for a in db.accounts_of(pid, 'dingtalk')
                if a.get('account_id')]
        if not uids:
            done.add(pid)      # 没有钉钉账号，发不了，直接丢掉，别堵队列
            continue
        title, md, url = _compose(evs, base)
        try:
            r = cli.notify_work(agent, uids, title, md, url=url)
            code = r.get('errcode') if isinstance(r, dict) else 0
            if code not in (0, None):
                raise RuntimeError('errcode=%s %s' % (code, (r or {}).get('errmsg')))
            sent += 1
            done.add(pid)
            time.sleep(0.15)
        except Exception as e:
            fails.append(str(e)[:160])
            # 失败退避：第 n 次失败后等 n*5 分钟再试；连续 12 次（约 6 小时）
            # 仍失败 → 转死信丢弃，只记一条日志，避免每 30 秒重试刷屏
            for ev in evs:
                t = int(ev.get('tries') or 0) + 1
                if t >= 12:
                    dropped += 1
                    continue
                ev['tries'] = t
                ev['rt'] = now_ms + min(t, 10) * 300 * 1000
                left.append(ev)
            if not fails[-1]:
                fails[-1] = 'unknown error'
    _save(left)

    if sent:
        n = sum(len(v) for k, v in groups.items() if k in done)
        try:
            db.log('notify', True, n, '钉钉消息：已推送 %d 人 / %d 项' % (sent, n))
        except Exception:
            pass
        if say:
            say('钉钉消息：已推送 %d 人 / %d 项' % (sent, n))
    if fails:
        try:
            db.log('notify', False, len(fails),
                   '钉钉消息发送失败（%d 条退避重试%s）：%s'
                   % (len(left), ('，丢弃 %d 条死信' % dropped) if dropped else '',
                      '；'.join(fails[:3])))
        except Exception:
            pass
    return sent


def flush_due():
    """定时器调用：攒 90 秒再发，避免本地几个脚本连续调用刷出多条"""
    return flush(min_age=60)
