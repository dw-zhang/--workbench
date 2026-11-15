# -*- coding: utf-8 -*-
"""钉钉机器人 AI 指令助手（服务端引擎）

入口：钉钉开发者后台「机器人」→ 消息接收地址 → /dingtalk/robot（HTTP 回调）

三层设计
1. 固定指令（帮助/待办/审批/抄送/@我/概览/资料/今天/本周）：
   直接查工作台数据，毫秒级回复，HTTP 回调里同步返回。
2. 自由文本 → 大模型（OpenAI 兼容接口）：
   模型通过工具（查审批/待办/等待回复/日程/资料/建任务/建日程）操作工作台，
   再组织成回复；回复走 sessionWebhook 异步发送，避免钉钉 10 秒回调超时。
3. 没配模型且不是固定指令 → 返回 None，走原有逻辑（落「等待回复」表）。

模型配置：工作台「应用接入」表里 应用标识=ai（或应用类型=AI 接口）的记录，
填 接口地址（如 https://api.siliconflow.cn/v1 或 https://api.deepseek.com）+
应用ID（模型名）+ 应用密钥（Key）即可，改完即时生效（无需重启）。
"""
import html as _html
import json
import re
import smtplib
import ssl
import threading
import time
import urllib.parse
import urllib.request
from email.header import Header
from email.mime.text import MIMEText
from email.utils import formataddr

import db

TABLES = db.TABLES
APPS_DB = TABLES['apps']

# ── 配置 ──────────────────────────────────────────────────────────────

PERSON_CFG_KEY = 'ai:person:%s'
GLOBAL_CFG_KEY = 'ai:global'


def person_cfg(pid):
    """读某人的个人 AI 配置（明文，仅本人/服务端内部用）；无则 None"""
    if not pid:
        return None
    try:
        raw = db.cfg_get(PERSON_CFG_KEY % pid)
        if not raw:
            return None
        c = json.loads(raw)
        if not isinstance(c, dict) or not c.get('base') or not c.get('key'):
            return None
        return c
    except Exception:
        return None


def ai_cfg(pid=''):
    """AI 接口配置：个人配置（ai:person:<pid>）优先 → 服务端全局配置（ai:global）→ 「应用接入」表全局配置
    全局配置放 cfg 存储（服务端 sqlite），不放应用接入表：那张表会下发浏览器，避免 Key 泄露给所有人"""
    c = person_cfg(pid)
    if c:
        return {'base': str(c['base']).strip().rstrip('/'),
                'key': str(c['key']).strip(),
                'model': str(c.get('model') or '').strip() or 'gpt-4o-mini',
                'name': str(c.get('name') or '我的 AI 模型'), 'src': 'person'}
    try:
        raw = db.cfg_get(GLOBAL_CFG_KEY)
        if raw:
            g = json.loads(raw)
            if isinstance(g, dict) and g.get('base') and g.get('key'):
                return {'base': str(g['base']).strip().rstrip('/'),
                        'key': str(g['key']).strip(),
                        'model': str(g.get('model') or '').strip() or 'gpt-4o-mini',
                        'name': str(g.get('name') or '全局 AI 模型'), 'src': 'global'}
    except Exception:
        pass
    try:
        rows = db.query(APPS_DB, size=1000)['results']
    except Exception:
        return None
    for r in rows:
        ident = str(r.get('应用标识') or '').strip().lower()
        typ = str(r.get('应用类型') or '').strip()
        if ident != 'ai' and typ != 'AI 接口':
            continue
        base = str(r.get('接口地址') or '').strip().rstrip('/')
        key = str(r.get('应用密钥') or '').strip()
        model = str(r.get('应用ID') or '').strip()
        if base and key:
            return {'base': base, 'key': key, 'model': model or 'gpt-4o-mini',
                    'name': str(r.get('应用名称') or 'AI 接口'), 'src': 'global'}
    return None


def person_of(body):
    """发送人钉钉 userId → 工作台 person_id + 姓名"""
    uid = str(body.get('senderStaffId') or '').strip()
    if not uid:
        uid = str(body.get('senderId') or '').strip()
    acc = db.account_find('dingtalk', uid)
    if not acc:
        return None, '', uid
    p = db.person_get(acc['person_id']) or {}
    return acc['person_id'], p.get('name') or '', uid


# ── 数据查询（按人过滤，管理员未绑定时兜底全量） ────────────────────

def _rows(database, pid, days=0, status='', keyword='', source='', limit=10):
    # 安全兜底：没有绑定人员的会话一律查不到数据（否则 person_id 为空会退化成全量）
    if not str(pid or '').strip():
        return []
    res = db.query(database, size=100000, person_id=pid)
    cut = time.strftime('%Y-%m-%d', time.localtime(time.time() - days * 86400)) if days else ''
    out = []
    for d in res['results']:
        if status and str(d.get('审批状态') or d.get('状态') or '').strip() != status:
            continue
        if source and str(d.get('审批来源') or '').strip() != source:
            continue
        if keyword:
            kw = keyword.lower()
            hay = ' '.join(str(v) for v in d.values() if v).lower()
            if kw not in hay:
                continue
        if cut:
            t = str(d.get('发起时间') or d.get('更新时间') or '')[:10]
            if t and t < cut:
                continue
        out.append(d)
        if len(out) >= 2000:
            break
    return out


def _day(v):
    return str(v or '')[:10]


def _appr_line(d):
    bits = [_day(d.get('发起时间')), str(d.get('审批标题') or '')[:28],
            str(d.get('审批状态') or ''), str(d.get('发起人') or '')]
    if d.get('金额'):
        bits.append('¥%s' % d['金额'])
    return ' · '.join(b for b in bits if b)


def _task_line(d):
    bits = [_day(d.get('截止时间')) or '未定期', str(d.get('任务名称') or '')[:30],
            str(d.get('状态') or '待处理'), str(d.get('优先级') or '中') + '优先级']
    return ' · '.join(bits)


def _wait_line(d):
    return '%s · %s · %s' % (_day(d.get('发起时间')), str(d.get('事项名称') or '')[:36],
                             str(d.get('发送人') or ''))


def _sched_line(d):
    return '%s · %s' % (str(d.get('开始时间') or '')[:16].replace('T', ' '),
                        str(d.get('日程事项') or '')[:30])


def _md(title, lines, more=''):
    if not lines:
        return '#### %s\n\n没有符合条件的数据。' % title
    body = '\n'.join('- %s' % x for x in lines[:10])
    tail = '\n\n（仅列前 10 条，完整列表见工作台）' if len(lines) > 10 else ''
    if more:
        body += '\n\n' + more
    return '#### %s\n\n%s%s' % (title, body, tail)


def _reply(text, title='工作台助手'):
    return {'msgtype': 'markdown', 'markdown': {'title': title, 'text': text}}


# ── 固定指令 ──────────────────────────────────────────────────────────

def cmd_help(name=''):
    who = ('，%s' % name) if name else ''
    return ('#### 工作台 AI 助手\n\n你好%s，直接发指令或用大白话提问：\n\n'
            '- **待办** —— 未完成任务清单\n'
            '- **今天** —— 今天到期任务 + 日程\n'
            '- **本周** —— 本周重点\n'
            '- **审批** —— 最近 7 天审批动态\n'
            '- **审批中** / **我发起** / **抄送** —— 按来源筛\n'
            '- **@我** / **谁找我** —— 等待回复的消息\n'
            '- **日报 / 周报 / 月报** —— 基于任务/审批/日程/群消息自动整理\n'
            '- **消息统计 / 群消息** —— @我消息按群/按人/按天统计\n'
            '- **资料 关键词** —— 找文档\n'
            '- **概览** —— 工作台统计\n\n'
            '还能真正干活（大白话直接说）：\n'
            '- **联网搜索** —— 搜一下 2026 年企业微信私有化方案\n'
            '- **发邮件** —— 给 xxx@company.com 发邮件，主题…正文…\n'
            '- **发钉钉消息** —— 告诉张三 明天下午对账\n'
            '- **提交审批** —— 帮我提交请假审批（先看有哪些模板）\n\n'
            '其他句子会交给大模型理解（需在「应用接入」配置 AI 接口）。' % who)


def cmd_todo(pid):
    rows = _rows(TABLES['task'], pid)
    open_rows = [r for r in rows if str(r.get('状态') or '') != '已完成']
    open_rows.sort(key=lambda r: _day(r.get('截止时间')))
    return _md('未完成任务（%d 条）' % len(open_rows), [_task_line(r) for r in open_rows])


def cmd_today(pid):
    t0 = time.strftime('%Y-%m-%d')
    tasks = [r for r in _rows(TABLES['task'], pid)
             if str(r.get('状态') or '') != '已完成' and _day(r.get('截止时间')) and _day(r.get('截止时间')) <= t0]
    tasks.sort(key=lambda r: _day(r.get('截止时间')))
    sched = [r for r in _rows(TABLES['sched'], pid) if _day(r.get('开始时间')) == t0]
    sched.sort(key=lambda r: str(r.get('开始时间') or ''))
    lines = [_sched_line(r) for r in sched] + [_task_line(r) for r in tasks]
    return _md('今天（%s）' % t0, lines)


def cmd_week(pid):
    cut = time.strftime('%Y-%m-%d', time.localtime(time.time() + 7 * 86400))
    t0 = time.strftime('%Y-%m-%d')
    tasks = [r for r in _rows(TABLES['task'], pid)
             if str(r.get('状态') or '') != '已完成'
             and ((t0 <= _day(r.get('截止时间')) <= cut) or str(r.get('优先级') or '') == '高')]
    tasks.sort(key=lambda r: _day(r.get('截止时间')))
    sched = [r for r in _rows(TABLES['sched'], pid) if t0 <= _day(r.get('开始时间')) <= cut]
    sched.sort(key=lambda r: str(r.get('开始时间') or ''))
    lines = [_sched_line(r) for r in sched] + [_task_line(r) for r in tasks]
    return _md('本周重点（未来 7 天）', lines)


def cmd_approval(pid, source='', status='', days=7):
    rows = _rows(TABLES['ding'], pid, days=days, source=source, status=status)
    rows.sort(key=lambda r: str(r.get('发起时间') or ''), reverse=True)
    label = source or '审批'
    if status:
        label += ' · ' + status
    return _md('%s（最近 %d 天 %d 条）' % (label, days, len(rows)),
               [_appr_line(r) for r in rows])


def cmd_wait(pid):
    rows = _rows(TABLES['wait'], pid, days=14, status='等待回复')
    rows.sort(key=lambda r: str(r.get('发起时间') or ''), reverse=True)
    return _md('等待回复（%d 条）' % len(rows), [_wait_line(r) for r in rows])


def cmd_files(pid, kw=''):
    rows = _rows(TABLES['files'], pid, keyword=kw)
    rows.sort(key=lambda r: str(r.get('更新时间') or ''), reverse=True)
    lines = ['%s · %s · %s' % (_day(r.get('更新时间')), str(r.get('文件名称') or '')[:26],
                               str(r.get('所属项目') or '未分类')) for r in rows]
    return _md('资料「%s」（%d 条）' % (kw or '最近', len(rows)), lines)


def cmd_overview(pid):
    st = db.stats(pid)
    wait_open = len(_rows(TABLES['wait'], pid, days=3650, status='等待回复'))
    appr = _rows(TABLES['ding'], pid, days=7)
    appr_ing = len([r for r in appr if str(r.get('审批状态') or '') == '审批中'])
    lines = [
        '任务：%d 条（未完成 %d）' % (st.get('task', 0), len([r for r in _rows(TABLES['task'], pid) if str(r.get('状态') or '') != '已完成'])),
        '等待回复：待回复 %d 条' % wait_open,
        '审批（近 7 天）：%d 条，其中审批中 %d 条' % (len(appr), appr_ing),
        '日程：%d 条 · 资料：%d 份' % (st.get('sched', 0), st.get('files', 0)),
    ]
    return _md('工作台概况', lines)


# ── 消息统计与日报/周报/月报 ──────────────────────────────────────────

def _msg_stats(pid, days=7):
    """统计钉钉群 @我 消息（数据源：等待回复表 = @我消息库，由本机 sync_atme 同步）"""
    rows = _rows(TABLES['wait'], pid, days=days, limit=2000)
    by_group, by_sender, by_day = {}, {}, {}
    open_n = 0
    for r in rows:
        g = str(r.get('群名称') or '（未知会话）')[:24]
        s = str(r.get('发送人') or '（未知）')[:16]
        d = _day(r.get('发起时间'))
        by_group[g] = by_group.get(g, 0) + 1
        by_sender[s] = by_sender.get(s, 0) + 1
        by_day[d] = by_day.get(d, 0) + 1
        if str(r.get('状态') or '') == '等待回复':
            open_n += 1
    def top(d, n):
        return [{'名称': k, '条数': v} for k, v in sorted(d.items(), key=lambda x: -x[1])[:n]]
    return {'统计天数': days, '消息总数': len(rows), '其中未回复': open_n,
            '按群TOP': top(by_group, 8), '按发送人TOP': top(by_sender, 8),
            '按天分布': sorted(by_day.items())}


def _work_report(pid, period='week'):
    """日报/周报/月报的聚合数据（真实工作台数据，供大模型撰写报告）"""
    days = {'day': 1, 'week': 7, 'month': 30}.get(period, 7)
    today = time.strftime('%Y-%m-%d')
    start = time.strftime('%Y-%m-%d', time.localtime(time.time() - days * 86400))
    end = time.strftime('%Y-%m-%d', time.localtime(time.time() + 7 * 86400))
    tasks = _rows(TABLES['task'], pid, limit=2000)
    due_in = [r for r in tasks if start <= _day(r.get('截止时间')) <= today]
    done_in = [r for r in due_in if str(r.get('状态') or '') == '已完成']
    open_tasks = [r for r in tasks if str(r.get('状态') or '') != '已完成']
    overdue = [r for r in open_tasks if _day(r.get('截止时间')) and _day(r.get('截止时间')) < today]
    high_pri = [r for r in open_tasks if str(r.get('优先级') or '') == '高']
    soon = [r for r in open_tasks if today < _day(r.get('截止时间')) <= end]
    src_n = {}
    for r in tasks:
        s = str(r.get('来源应用') or '手动')[:12]
        src_n[s] = src_n.get(s, 0) + 1
    appr = _rows(TABLES['ding'], pid, days=days, limit=500)
    def _appr_by(src):
        rows = [r for r in appr if str(r.get('审批来源') or '') == src]
        return {'条数': len(rows), '明细': [{'日期': _day(r.get('发起时间')),
                 '标题': str(r.get('审批标题') or '')[:30], '状态': r.get('审批状态'),
                 '金额': r.get('金额')} for r in rows[:15]]}
    sched = [r for r in _rows(TABLES['sched'], pid, limit=500)
             if start <= _day(r.get('开始时间')) <= today]
    msg = _msg_stats(pid, days=days)
    mrows = _rows(TABLES['wait'], pid, days=days, limit=2000)
    msg['明细'] = [{'日期': _day(r.get('发起时间')), '群': str(r.get('群名称') or '')[:16],
                    '发送人': str(r.get('发送人') or '')[:12],
                    '内容': str(r.get('消息内容') or '')[:40], '状态': str(r.get('状态') or '')}
                   for r in mrows[:40]]
    # 与我相关的待跟进事项（不限统计区间）
    open_waits = _rows(TABLES['wait'], pid, days=3650, limit=500, status='等待回复')
    appr_ing_all = [r for r in appr if str(r.get('审批状态') or '') == '审批中']
    follow_up = {
        '未回复的@我消息': [{'日期': _day(r.get('发起时间')), '群': str(r.get('群名称') or '')[:16],
                            '发送人': str(r.get('发送人') or '')[:12],
                            '内容': str(r.get('消息内容') or '')[:40]}
                           for r in open_waits[:10]],
        '审批中(需我跟进)': [{'日期': _day(r.get('发起时间')), '标题': str(r.get('审批标题') or '')[:30],
                              '来源': r.get('审批来源')} for r in appr_ing_all[:10]],
    }
    return {
        '报告类型': {'day': '日报', 'week': '周报', 'month': '月报'}.get(period, '周报'),
        '统计区间': '%s ~ %s' % (start, today),
        '范围说明': '以下全部为与本人相关的钉钉信息：任务=我负责/钉钉多维表格@我的，审批=我发起/我已处理/抄送我的，日程=我的日程，消息=群里@我的',
        '待我跟进（跨区间汇总）': follow_up,
        '任务': {
            '区间内到期': len(due_in), '其中已完成': len(done_in),
            '当前未完成总数': len(open_tasks), '逾期未完成': len(overdue),
            '高优先级未完成': len(high_pri),
            '来源分布': src_n,
            '未完成明细': [{'截止': _day(r.get('截止时间')), '任务': str(r.get('任务名称'))[:30],
                            '优先级': str(r.get('优先级') or ''), '来源': str(r.get('来源应用') or '手动')[:10]}
                           for r in (overdue + high_pri + open_tasks)[:15]],
            '区间内已完成明细': [{'截止': _day(r.get('截止时间')), '任务': str(r.get('任务名称'))[:30],
                                '来源': str(r.get('来源应用') or '手动')[:10]}
                               for r in done_in[:15]],
            '未来7天到期': [{'截止': _day(r.get('截止时间')), '任务': str(r.get('任务名称'))[:30]}
                          for r in soon[:10]],
        },
        '审批': {'区间内动态': len(appr), '我发起': _appr_by('我发起'),
                 '抄送我': _appr_by('抄送我'), '我已处理': _appr_by('我已处理'),
                 '审批中条数': len([r for r in appr if str(r.get('审批状态') or '') == '审批中'])},
        '日程': {'区间内个数': len(sched), '明细': [_sched_line(r) for r in sched[:15]]},
        '钉钉群消息@我': msg,
    }


# ── 联网 / 邮件 / 钉钉写操作 ─────────────────────────────────────────

_UA = {'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) '
                     'AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0 Safari/537.36',
       'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
       'Accept-Language': 'zh-CN,zh;q=0.9,en;q=0.8'}


def _http_get(url, timeout=15, max_bytes=600000, referer=''):
    if not str(url).lower().startswith(('http://', 'https://')):
        url = 'https://' + str(url)
    hdr = dict(_UA)
    if referer:
        hdr['Referer'] = referer
    req = urllib.request.Request(url, headers=hdr)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read(max_bytes)
            ctype = r.headers.get('Content-Type', '') or ''
    except urllib.error.HTTPError as e:
        raise RuntimeError('HTTP %s（对方站点拒绝抓取，可换搜索结果里的其他链接）' % e.code)
    enc = 'utf-8'
    m = re.search(r'charset=([\w-]+)', ctype)
    if m:
        enc = m.group(1)
    try:
        return raw.decode(enc, 'ignore')
    except LookupError:
        return raw.decode('utf-8', 'ignore')


def _html_text(s):
    s = re.sub(r'(?is)<(script|style|noscript)[^>]*>.*?</\1>', ' ', s or '')
    s = re.sub(r'(?is)<br\s*/?>|</p>|</div>|</li>|</tr>|</h[1-6]>', '\n', s)
    s = re.sub(r'(?s)<[^>]+>', ' ', s)
    s = _html.unescape(s)
    s = re.sub(r'[ \t\u00a0]+', ' ', s)
    s = re.sub(r'\n\s*\n+', '\n', s)
    return s.strip()


def _parse_h3_results(page, n=6, base=''):
    out, seen = [], set()
    for m in re.finditer(r'(?is)<h3[^>]*>\s*<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>', page):
        title = _html_text(m.group(2))[:90]
        href = m.group(1)
        if href.startswith('/'):
            href = base.rstrip('/') + href
        if not title or title in seen:
            continue
        seg = _html_text(page[m.end():m.end() + 1500])[:170]
        seen.add(title)
        out.append({'标题': title, '链接': href, '摘要': seg})
        if len(out) >= n:
            break
    return out


def _web_search(q, n=6):
    """联网搜索：百度 → 搜狗 → 必应 依次尝试"""
    qs = urllib.parse.quote(str(q))
    tries = [
        ('百度', 'https://www.baidu.com/s?wd=%s&rn=10' % qs, 'https://www.baidu.com',
         'https://www.baidu.com/'),
        ('搜狗', 'https://www.sogou.com/web?query=%s' % qs, 'https://www.sogou.com',
         'https://www.sogou.com/'),
        ('必应', 'https://cn.bing.com/search?q=%s&mkt=zh-CN&ensearch=0' % qs, 'https://cn.bing.com',
         'https://cn.bing.com/'),
    ]
    errs = []
    for name, url, base, ref in tries:
        try:
            page = _http_get(url, referer=ref)
            res = _parse_h3_results(page, n, base)
            if res:
                return res
            errs.append('%s无结果' % name)
        except Exception as e:
            errs.append('%s:%s' % (name, str(e)[:60]))
    return [{'标题': '搜索失败', '链接': '', '摘要': '；'.join(errs)}]


def _fetch_url(url, max_chars=4000):
    txt = _html_text(_http_get(url))
    return txt[:max_chars]


# —— 邮件（SMTP，个人配置优先，其次全局 mail:smtp）——

def mail_cfg(pid=''):
    raw = None
    if pid:
        raw = db.cfg_get('mail:person:%s' % pid)
    if not raw:
        raw = db.cfg_get('mail:smtp')
    if not raw:
        return None
    try:
        c = json.loads(raw) if isinstance(raw, str) else dict(raw)
    except Exception:
        return None
    if not (c.get('host') and c.get('user') and c.get('pass')):
        return None
    return c


def _send_email(pid, to, subject, body, cc=''):
    c = mail_cfg(pid)
    if not c:
        return {'ok': False, 'error': '未配置发件邮箱',
                'hint': '需要先在服务器配置 SMTP（键 mail:smtp 或 mail:person:<pid>），字段 host/port/user/pass/sender'}
    sender = str(c.get('sender') or c.get('user'))
    msg = MIMEText(str(body), 'plain', 'utf-8')
    msg['Subject'] = Header(str(subject), 'utf-8')
    msg['From'] = formataddr((str(c.get('name') or '工作台 AI 助手'), sender))
    msg['To'] = ', '.join([x.strip() for x in str(to).replace('；', ';').replace(',', ';').split(';') if x.strip()])
    if cc:
        msg['Cc'] = cc
    host = str(c['host'])
    port = int(c.get('port') or 465)
    try:
        if c.get('ssl', True):
            srv = smtplib.SMTP_SSL(host, port, timeout=20, context=ssl.create_default_context())
        else:
            srv = smtplib.SMTP(host, port, timeout=20)
            try:
                srv.starttls(context=ssl.create_default_context())
            except Exception:
                pass
        srv.login(str(c['user']), str(c['pass']))
        rcpts = [x.strip() for x in str(to).replace('；', ';').replace(',', ';').split(';') if x.strip()]
        srv.sendmail(sender, rcpts, msg.as_string())
        srv.quit()
        return {'ok': True, '收件人': rcpts, '主题': str(subject)}
    except Exception as e:
        return {'ok': False, 'error': '%s: %s' % (type(e).__name__, str(e)[:200])}


# —— 钉钉（应用凭证已配置：发工作通知 / 建待办 / 发起审批）——

def _ding_conf():
    def g(k):
        return db.cfg_get(k) or ''
    return {'app_key': g('dingtalk:app_key'), 'app_secret': g('dingtalk:app_secret'),
            'corp_id': g('dingtalk:corp_id'), 'agent_id': g('dingtalk:agent_id') or g('agent_id'),
            'public_url': g('dingtalk:public_url')}


def _ding_client():
    import dingtalk
    c = _ding_conf()
    if not (c['app_key'] and c['app_secret']):
        return None
    return dingtalk.Client(c['app_key'], c['app_secret'], c['corp_id'])


def _staff_of(name):
    """按姓名找钉钉 userid（staffId）"""
    name = str(name or '').strip()
    if not name:
        return None, None
    for p in db.persons_all():
        if str(p.get('name') or '') == name:
            for a in db.accounts_of(p['person_id']):
                if a.get('platform') == 'dingtalk' and a.get('account_id'):
                    return a['account_id'], p['person_id']
    for p in db.persons_all():
        if name and name in str(p.get('name') or ''):
            for a in db.accounts_of(p['person_id']):
                if a.get('platform') == 'dingtalk' and a.get('account_id'):
                    return a['account_id'], p['person_id']
    return None, None


def _send_ding_msg(pid, to, text, title=''):
    cli = _ding_client()
    if not cli:
        return {'ok': False, 'error': '钉钉应用凭证未配置'}
    uid, _ = _staff_of(to)
    if not uid:
        return {'ok': False, 'error': '找不到同事「%s」的钉钉账号' % to}
    agent = _ding_conf().get('agent_id')
    if not agent:
        return {'ok': False, 'error': '缺少 AgentId 配置'}
    try:
        cli.notify_work(agent, [uid], title or '来自工作台 AI 助手', str(text)[:1500],
                        _ding_conf().get('public_url') or '')
        return {'ok': True, '收件人': to, '内容': str(text)[:120]}
    except Exception as e:
        return {'ok': False, 'error': '%s: %s' % (type(e).__name__, str(e)[:200])}


def _tpl_cache_map():
    """cfg 缓存的模板映射（名称→process_code）。来源：权限开通后模板接口刷新，或人工登记"""
    try:
        return json.loads(db.cfg_get('ai:approval_tpls') or '{}') or {}
    except Exception:
        return {}


def _tpl_cache_refresh():
    """模板接口可用时刷新缓存（模板变更后自动同步）"""
    cli = _ding_client()
    if not cli:
        return
    url = 'https://oapi.dingtalk.com/topapi/process/template/list?access_token=' + cli.token()
    body = json.dumps({'userid': _ding_conf().get('admin_userid') or ''}).encode()
    req = urllib.request.Request(url, data=body, headers={'Content-Type': 'application/json'})
    try:
        d = json.loads(urllib.request.urlopen(req, timeout=15).read().decode())
    except Exception:
        return
    if d.get('errcode'):
        return
    res = (d.get('result') or {}).get('result') or []
    m = {str(x.get('name') or ''): str(x.get('process_code') or '')
         for x in res if x.get('name') and x.get('process_code')}
    if m:
        db.cfg_set({'ai:approval_tpls': json.dumps(m, ensure_ascii=False)})


def _approval_templates():
    """审批模板列表：接口权限可用走接口，不可用回退 cfg 缓存"""
    cli = _ding_client()
    if not cli:
        return {'ok': False, 'error': '钉钉应用凭证未配置'}
    url = 'https://oapi.dingtalk.com/topapi/process/template/list?access_token=' + cli.token()
    body = json.dumps({'userid': _ding_conf().get('admin_userid') or ''}).encode()
    req = urllib.request.Request(url, data=body, headers={'Content-Type': 'application/json'})
    try:
        d = json.loads(urllib.request.urlopen(req, timeout=15).read().decode())
    except Exception as e:
        d = {'errcode': -1, 'errmsg': str(e)[:160]}
    if not d.get('errcode'):
        _tpl_cache_refresh()
        res = (d.get('result') or {}).get('result') or []
        return {'ok': True, '模板': [{'名称': x.get('name'), 'process_code': x.get('process_code')}
                                    for x in res][:40]}
    cached = _tpl_cache_map()
    if cached:
        return {'ok': True, '模板': [{'名称': k, 'process_code': v} for k, v in cached.items()],
                '来源': '本地缓存（接口权限未开通，结果可能不全）'}
    return {'ok': False, 'errcode': d.get('errcode'), 'error': str(d.get('errmsg'))[:300],
            'hint': '需要在钉钉开放平台给应用开通「审批」权限，或先在服务器上登记模板编号'}


def _submit_approval(pid, template, fields, approvers, title=''):
    """发起钉钉 OA 审批。fields: [{'name':'字段名','value':'值'}]，approvers: 姓名列表"""
    cli = _ding_client()
    if not cli:
        return {'ok': False, 'error': '钉钉应用凭证未配置'}
    tpl = _approval_templates()
    if not tpl.get('ok'):
        return tpl
    code = str(template or '').strip()
    if not code.upper().startswith('PROC-'):
        hit = [t for t in tpl.get('模板', []) if code and code in str(t.get('名称') or '')]
        if not hit:
            return {'ok': False, 'error': '找不到审批模板「%s」' % code,
                    '可用模板': [t['名称'] for t in tpl.get('模板', [])][:20]}
        code = hit[0]['process_code']
    uid = _ding_conf().get('admin_userid')
    for a in db.accounts_of(pid):
        if a.get('platform') == 'dingtalk' and a.get('account_id'):
            uid = a['account_id']
    apv = []
    for n in (approvers or []):
        u, _p = _staff_of(n)
        if u:
            apv.append(u)
    if not apv:
        return {'ok': False, 'error': '审批人未解析到（请用同事姓名）'}
    dept = 0
    try:
        dept = int((json.loads(db.cfg_get('dingtalk:dept_ids') or '[]') or [1])[0])
    except Exception:
        dept = 1
    payload = {'process_code': code, 'originator_user_id': uid, 'dept_id': dept,
               'approvers': ','.join(apv),
               'form_component_values': [{'name': str(f.get('name'))[:60],
                                          'value': str(f.get('value'))[:500]}
                                         for f in (fields or [])]}
    if title:
        payload['form_component_values'].append({'name': '标题', 'value': str(title)[:60]})
    url = 'https://oapi.dingtalk.com/topapi/processinstance/create?access_token=' + cli.token()
    req = urllib.request.Request(url, data=json.dumps(payload, ensure_ascii=False).encode(),
                                 headers={'Content-Type': 'application/json'})
    try:
        d = json.loads(urllib.request.urlopen(req, timeout=20).read().decode())
    except Exception as e:
        return {'ok': False, 'error': str(e)[:160]}
    if d.get('errcode'):
        return {'ok': False, 'errcode': d.get('errcode'), 'error': str(d.get('errmsg'))[:300],
                'hint': '若提示权限不足，需在钉钉开放平台为应用开通「审批」相关权限'}
    return {'ok': True, '审批单号': (d.get('result') or {}).get('process_instance_id'),
            '模板': code, '审批人': approvers}


FAST_PATTERNS = [
    (re.compile(r'^(帮助|help|指令|菜单|你能做什么|你会什么|怎么用)\s*$', re.I), 'help'),
    (re.compile(r'^(概览|统计|工作台概况|工作台统计)\s*$'), 'overview'),
    (re.compile(r'^(我的)?待办|^(任务清单|未完成任务)$'), 'todo'),
    (re.compile(r'^(今天|今日)(有什么)?(安排|日程|任务)?\s*$|^(今天|今日)要(干|做)什么$'), 'today'),
    (re.compile(r'^(本周|这周)(重点|安排|有什么)?\s*$'), 'week'),
    (re.compile(r'^(审批中|在审)'), 'approval_ing'),
    (re.compile(r'^(我发起|我提交|我发起的审批)'), 'approval_mine'),
    (re.compile(r'^(抄送|抄送我)(的审批)?(最近)?\s*(\d+)?(天)?\s*$'), 'approval_cc'),
    (re.compile(r'^(审批|审批动态|最近审批)(最近)?\s*(\d+)?(天)?\s*$'), 'approval_all'),
    (re.compile(r'^(@我|谁找我|找我的事|等待回复|谁在等我)'), 'wait'),
    (re.compile(r'^(资料|文档|搜|找)(文件|资料|文档)?\s*[:：]?\s*(.*)$'), 'files'),
]


def fast_reply(content, pid, name):
    """固定指令 → markdown 文本；不是指令返回 None"""
    t = content.strip()
    for pat, act in FAST_PATTERNS:
        m = pat.match(t)
        if not m:
            continue
        if act == 'help':
            return cmd_help(name)
        if act == 'overview':
            return cmd_overview(pid)
        if act == 'todo':
            return cmd_todo(pid)
        if act == 'today':
            return cmd_today(pid)
        if act == 'week':
            return cmd_week(pid)
        if act == 'approval_ing':
            return cmd_approval(pid, status='审批中', days=14)
        if act == 'approval_mine':
            return cmd_approval(pid, source='我发起', days=30)
        if act == 'approval_cc':
            days = int(m.group(4) or 0) or 7
            return cmd_approval(pid, source='抄送我', days=days)
        if act == 'approval_all':
            days = int(m.group(3) or 0) or 7
            return cmd_approval(pid, days=days)
        if act == 'wait':
            return cmd_wait(pid)
        if act == 'files':
            kw = (m.group(3) or '').strip()
            if not kw:
                kw = ''
            return cmd_files(pid, kw)
    return None


# ── 大模型（OpenAI 兼容 + 工具调用） ─────────────────────────────────

LLM_TOOLS = [
    {'type': 'function', 'function': {'name': 'query_approvals',
        'description': '查询钉钉审批数据（我发起/抄送我/我已处理，含状态与金额）',
        'parameters': {'type': 'object', 'properties': {
            'days': {'type': 'integer', 'description': '最近多少天，默认 14'},
            'source': {'type': 'string', 'description': '审批来源：我发起/抄送我/我已处理，可空'},
            'status': {'type': 'string', 'description': '审批状态：审批中/已通过/已拒绝，可空'},
            'keyword': {'type': 'string', 'description': '标题关键词，可空'}}, 'required': []}}},
    {'type': 'function', 'function': {'name': 'query_todo',
        'description': '查询任务表（待办）',
        'parameters': {'type': 'object', 'properties': {
            'open_only': {'type': 'boolean', 'description': '只看未完成，默认 true'},
            'keyword': {'type': 'string', 'description': '关键词，可空'}}, 'required': []}}},
    {'type': 'function', 'function': {'name': 'query_wait',
        'description': '查询「等待回复」消息（别人找我、群里@我）',
        'parameters': {'type': 'object', 'properties': {
            'status': {'type': 'string', 'description': '等待回复/已回复，默认 等待回复'},
            'days': {'type': 'integer', 'description': '最近多少天，默认 14'}}, 'required': []}}},
    {'type': 'function', 'function': {'name': 'query_schedule',
        'description': '查询日程安排',
        'parameters': {'type': 'object', 'properties': {
            'days': {'type': 'integer', 'description': '未来/近多少天，默认 7'}}, 'required': []}}},
    {'type': 'function', 'function': {'name': 'query_files',
        'description': '在资料表里找文档',
        'parameters': {'type': 'object', 'properties': {
            'keyword': {'type': 'string', 'description': '关键词'}}, 'required': []}}},
    {'type': 'function', 'function': {'name': 'create_task',
        'description': '新建任务到任务表',
        'parameters': {'type': 'object', 'properties': {
            'title': {'type': 'string', 'description': '任务名称'},
            'priority': {'type': 'string', 'enum': ['高', '中', '低']},
            'due_date': {'type': 'string', 'description': '截止日期 YYYY-MM-DD，可空'},
            'project': {'type': 'string', 'description': '所属项目，可空'},
            'owner': {'type': 'string', 'description': '负责人，可空'}},
            'required': ['title']}}},
    {'type': 'function', 'function': {'name': 'create_schedule',
        'description': '新建日程（自动写入工作台并回传钉钉日历）',
        'parameters': {'type': 'object', 'properties': {
            'title': {'type': 'string', 'description': '日程事项'},
            'start': {'type': 'string', 'description': '开始时间 YYYY-MM-DDTHH:MM:00'},
            'hours': {'type': 'number', 'description': '时长（小时），默认 1'}},
            'required': ['title', 'start']}}},
    {'type': 'function', 'function': {'name': 'overview',
        'description': '工作台整体统计', 'parameters': {'type': 'object', 'properties': {}}}},
    {'type': 'function', 'function': {'name': 'complete_task',
        'description': '把一条任务标记为已完成（按任务名称模糊匹配）',
        'parameters': {'type': 'object', 'properties': {
            'title': {'type': 'string', 'description': '任务名称（支持部分匹配）'}},
            'required': ['title']}}},
    {'type': 'function', 'function': {'name': 'get_weather',
        'description': '查询中国城市今天和明天的天气（温度/天气现象）',
        'parameters': {'type': 'object', 'properties': {
            'city': {'type': 'string', 'description': '城市名，如 深圳/上海/杭州'}},
            'required': ['city']}}},
    {'type': 'function', 'function': {'name': 'msg_stats',
        'description': '统计钉钉群消息（@我的消息库：按群/按发送人/按天分布，含未回复数）',
        'parameters': {'type': 'object', 'properties': {
            'days': {'type': 'integer', 'description': '统计最近多少天，默认 7'}}, 'required': []}}},
    {'type': 'function', 'function': {'name': 'work_report',
        'description': '获取日报/周报/月报的聚合数据（任务完成/逾期/审批/日程/群消息统计），调用后基于返回的真实数据撰写报告，不要编造',
        'parameters': {'type': 'object', 'properties': {
            'period': {'type': 'string', 'enum': ['day', 'week', 'month'],
                       'description': 'day=日报 week=周报 month=月报'}},
            'required': ['period']}}},
    {'type': 'function', 'function': {'name': 'web_search',
        'description': '联网搜索互联网实时信息（必应）。查政策/行业资讯/技术方案/公司信息等一切工作台以外的信息时用',
        'parameters': {'type': 'object', 'properties': {
            'query': {'type': 'string', 'description': '搜索关键词'}},
            'required': ['query']}}},
    {'type': 'function', 'function': {'name': 'fetch_url',
        'description': '读取指定网页的正文内容（用于查看搜索结果详情、公告、文档页）',
        'parameters': {'type': 'object', 'properties': {
            'url': {'type': 'string', 'description': '完整网址 http(s)://...'}},
            'required': ['url']}}},
    {'type': 'function', 'function': {'name': 'send_email',
        'description': '发送电子邮件（需已配置发件邮箱）。用户要求发邮件、通知外部人员、发报告时使用',
        'parameters': {'type': 'object', 'properties': {
            'to': {'type': 'string', 'description': '收件人邮箱，多个用分号分隔'},
            'subject': {'type': 'string', 'description': '邮件主题'},
            'body': {'type': 'string', 'description': '邮件正文（纯文本）'},
            'cc': {'type': 'string', 'description': '抄送，可空'}},
            'required': ['to', 'subject', 'body']}}},
    {'type': 'function', 'function': {'name': 'send_dingtalk_msg',
        'description': '给同事发钉钉工作通知消息（1对1推送，不是群发）。需要转告/催办/通知某人时使用',
        'parameters': {'type': 'object', 'properties': {
            'to': {'type': 'string', 'description': '同事姓名（钉钉通讯录里的名字）'},
            'text': {'type': 'string', 'description': '消息内容'},
            'title': {'type': 'string', 'description': '消息标题，可空'}},
            'required': ['to', 'text']}}},
    {'type': 'function', 'function': {'name': 'submit_approval',
        'description': '发起钉钉 OA 审批（如请假/报销/采购/用印）。不传 template 时先列出可用审批模板',
        'parameters': {'type': 'object', 'properties': {
            'template': {'type': 'string', 'description': '审批模板名称或 process_code，可空（空=列出模板）'},
            'fields': {'type': 'array', 'description': '表单字段：[{"name":"字段名","value":"值"}]',
                       'items': {'type': 'object', 'properties': {
                           'name': {'type': 'string'}, 'value': {'type': 'string'}}}},
            'approvers': {'type': 'array', 'description': '审批人姓名列表',
                          'items': {'type': 'string'}},
            'title': {'type': 'string', 'description': '审批标题，可空'}},
            'required': []}}},
]

_WMO = {0: '晴', 1: '大致晴', 2: '多云', 3: '阴', 45: '雾', 48: '雾凇',
        51: '小毛雨', 53: '毛雨', 55: '大毛雨', 61: '小雨', 63: '中雨', 65: '大雨',
        66: '冻雨', 67: '强冻雨', 71: '小雪', 73: '中雪', 75: '大雪', 77: '雪粒',
        80: '小阵雨', 81: '阵雨', 82: '强阵雨', 85: '小阵雪', 86: '大阵雪',
        95: '雷阵雨', 96: '雷阵雨伴冰雹', 99: '强雷阵雨伴冰雹'}


def _get_weather(city):
    """Open-Meteo 免费 API：城市 → 经纬度 → 今明两天天气"""
    import urllib.parse
    q = urllib.parse.quote(str(city or '').strip())
    geo = 'https://geocoding-api.open-meteo.com/v1/search?name=%s&count=1&language=zh' % q
    with urllib.request.urlopen(geo, timeout=15) as r:
        g = json.load(r)
    res = (g.get('results') or [None])[0]
    if not res:
        return {'error': '找不到城市：%s' % city}
    url = ('https://api.open-meteo.com/v1/forecast?latitude=%s&longitude=%s'
           '&daily=temperature_2m_max,temperature_2m_min,weathercode,precipitation_probability_max'
           '&timezone=Asia%%2FShanghai&forecast_days=2'
           % (res['latitude'], res['longitude']))
    with urllib.request.urlopen(url, timeout=15) as r:
        d = json.load(r)
    day = d.get('daily') or {}
    out = {'城市': '%s（%s%s）' % (res.get('name'), res.get('admin1') or '', res.get('country') or '')}
    for i, t in enumerate((day.get('time') or [])[:2]):
        label = '今天' if i == 0 else '明天'
        code = (day.get('weathercode') or [0, 0])[i]
        out[label] = {
            '日期': t,
            '天气': _WMO.get(code, '未知(%s)' % code),
            '最低温': '%.0f℃' % day['temperature_2m_min'][i],
            '最高温': '%.0f℃' % day['temperature_2m_max'][i],
            '降水概率': '%d%%' % ((day.get('precipitation_probability_max') or [0, 0])[i] or 0),
        }
    return out


def _tool_exec(name, args, pid):
    args = args or {}
    if name == 'web_search':
        try:
            res = _web_search(args.get('query') or '', n=6)
            return json.dumps({'query': args.get('query'), '结果': res} if res
                              else {'query': args.get('query'), '结果': [], 'note': '无结果，可换关键词或直接用知识回答'},
                              ensure_ascii=False)
        except Exception as e:
            return json.dumps({'ok': False, 'error': '%s: %s' % (type(e).__name__, str(e)[:120])},
                              ensure_ascii=False)
    if name == 'fetch_url':
        try:
            return json.dumps({'url': args.get('url'), '正文': _fetch_url(args.get('url') or '')},
                              ensure_ascii=False)
        except Exception as e:
            return json.dumps({'ok': False, 'error': '%s: %s' % (type(e).__name__, str(e)[:120])},
                              ensure_ascii=False)
    if name == 'send_email':
        return json.dumps(_send_email(pid, args.get('to') or '', args.get('subject') or '',
                                      args.get('body') or '', args.get('cc') or ''),
                          ensure_ascii=False)
    if name == 'send_dingtalk_msg':
        return json.dumps(_send_ding_msg(pid, args.get('to') or '', args.get('text') or '',
                                         args.get('title') or ''), ensure_ascii=False)
    if name == 'submit_approval':
        if not args.get('template'):
            return json.dumps(_approval_templates(), ensure_ascii=False)
        return json.dumps(_submit_approval(pid, args.get('template'), args.get('fields') or [],
                                           args.get('approvers') or [], args.get('title') or ''),
                          ensure_ascii=False)
    if name == 'msg_stats':
        return json.dumps(_msg_stats(pid, days=int(args.get('days') or 7)), ensure_ascii=False)
    if name == 'work_report':
        return json.dumps(_work_report(pid, str(args.get('period') or 'week')), ensure_ascii=False)
    if name == 'query_approvals':
        rows = _rows(TABLES['ding'], pid, days=int(args.get('days') or 14),
                     status=str(args.get('status') or ''), keyword=str(args.get('keyword') or ''),
                     source=str(args.get('source') or ''), limit=30)
        rows.sort(key=lambda r: str(r.get('发起时间') or ''), reverse=True)
        return json.dumps([{'日期': _day(r.get('发起时间')), '标题': r.get('审批标题'),
                            '状态': r.get('审批状态'), '来源': r.get('审批来源'),
                            '发起人': r.get('发起人'), '金额': r.get('金额')} for r in rows[:30]],
                          ensure_ascii=False)
    if name == 'query_todo':
        rows = _rows(TABLES['task'], pid, keyword=str(args.get('keyword') or ''))
        if args.get('open_only', True):
            rows = [r for r in rows if str(r.get('状态') or '') != '已完成']
        rows.sort(key=lambda r: _day(r.get('截止时间')))
        return json.dumps([{'截止': _day(r.get('截止时间')), '任务': r.get('任务名称'),
                            '状态': r.get('状态'), '优先级': r.get('优先级'),
                            '负责人': r.get('负责人')} for r in rows[:30]], ensure_ascii=False)
    if name == 'query_wait':
        rows = _rows(TABLES['wait'], pid, days=int(args.get('days') or 14),
                     status=str(args.get('status') or '等待回复'))
        rows.sort(key=lambda r: str(r.get('发起时间') or ''), reverse=True)
        return json.dumps([{'日期': _day(r.get('发起时间')), '事项': str(r.get('事项名称'))[:60],
                            '状态': r.get('状态'), '发送人': r.get('发送人')} for r in rows[:30]],
                          ensure_ascii=False)
    if name == 'query_schedule':
        days = int(args.get('days') or 7)
        rows = _rows(TABLES['sched'], pid)
        t0 = time.strftime('%Y-%m-%d')
        out = [r for r in rows if t0 <= _day(r.get('开始时间')) <= time.strftime(
            '%Y-%m-%d', time.localtime(time.time() + days * 86400))]
        out.sort(key=lambda r: str(r.get('开始时间') or ''))
        return json.dumps([{'开始': r.get('开始时间'), '事项': r.get('日程事项')} for r in out[:30]],
                          ensure_ascii=False)
    if name == 'query_files':
        rows = _rows(TABLES['files'], pid, keyword=str(args.get('keyword') or ''))
        return json.dumps([{'名称': r.get('文件名称'), '知识库': r.get('知识库'),
                            '路径': r.get('层级路径'), '类型': r.get('类型'),
                            '更新': _day(r.get('更新时间'))} for r in rows[:20]], ensure_ascii=False)
    if name == 'create_task':
        props = {'任务名称': str(args.get('title') or '')[:60],
                 '优先级': args.get('priority') or '中',
                 '状态': '待处理', '来源应用': 'AI助手'}
        if args.get('due_date'):
            props['截止时间'] = str(args['due_date'])[:10] + 'T18:00:00'
        if args.get('project'):
            props['所属项目'] = str(args['project'])[:40]
        if args.get('owner'):
            props['负责人'] = str(args['owner'])[:20]
        rid = db.add_record(TABLES['task'], props, source='ai_bot', person_id=pid)
        return json.dumps({'ok': True, '任务': props['任务名称'], 'id': rid}, ensure_ascii=False)
    if name == 'create_schedule':
        start = str(args.get('start') or '')
        if len(start) == 16:
            start += ':00'
        title = str(args.get('title') or '')[:60]
        try:
            hours = float(args.get('hours') or 1)
        except (TypeError, ValueError):
            hours = 1.0
        rid = db.add_record(TABLES['sched'],
                            {'日程事项': title, '开始时间': start,
                             '来源应用': 'AI助手'}, source='ai_bot', person_id=pid)
        # 回传钉钉日历
        pushed = False
        try:
            import datetime as _dt
            sd = _dt.datetime.strptime(start, '%Y-%m-%dT%H:%M:%S')
            ed = sd + _dt.timedelta(hours=hours)
            cli = _ding_client()
            if cli:
                acc = next((a for a in db.accounts_of(pid)
                            if a.get('platform') == 'dingtalk' and a.get('account_id')), None)
                if acc:
                    uni = cli.user_by_id(acc['account_id']).get('unionid') or ''
                    if uni:
                        eid = cli.event_create(uni, title,
                                               sd.strftime('%Y-%m-%dT%H:%M:%S'),
                                               ed.strftime('%Y-%m-%dT%H:%M:%S'))
                        if eid:
                            pushed = True
                            db.update_record(TABLES['sched'], rid, {'事件ID': eid},
                                             person_id=pid)
        except Exception:
            pass
        return json.dumps({'ok': True, '日程': title, '开始': start, 'id': rid,
                           '钉钉日历': '已同步' if pushed else '已写入工作台（钉钉日历未同步）'},
                          ensure_ascii=False)
    if name == 'overview':
        return json.dumps(json.loads(_strip_md(cmd_overview(pid))), ensure_ascii=False)
    if name == 'complete_task':
        title = str(args.get('title') or '').strip()
        rows = [r for r in _rows(TABLES['task'], pid, keyword=title)
                if str(r.get('状态') or '') != '已完成']
        if not rows:
            return json.dumps({'ok': False, 'error': '没找到未完成的任务：%s' % title}, ensure_ascii=False)
        # 取标题最接近的一条
        best = min(rows, key=lambda r: 0 if title in str(r.get('任务名称') or '') else len(str(r.get('任务名称') or '')))
        rid = best.get('_id') or best.get('record_id') or best.get('id')
        db.update_record(TABLES['task'], rid, {'状态': '已完成'}, person_id=pid)
        return json.dumps({'ok': True, '已完成任务': best.get('任务名称'),
                           '原状态': best.get('状态'), 'id': rid}, ensure_ascii=False)
    if name == 'get_weather':
        return json.dumps(_get_weather(args.get('city')), ensure_ascii=False)
    return json.dumps({'error': '未知工具 %s' % name}, ensure_ascii=False)


def _strip_md(text):
    """把固定指令的 markdown 输出转成 JSON 交给模型（取行内容）"""
    lines = []
    for ln in str(text).split('\n'):
        ln = ln.strip()
        if ln.startswith('- '):
            lines.append(ln[2:])
        elif ln.startswith('#### '):
            lines.append(ln[5:])
    return json.dumps(lines, ensure_ascii=False)


def _chat_once(cfg, messages, tools=True):
    body = {'model': cfg['model'], 'messages': messages, 'temperature': 0.3}
    if tools:
        body['tools'] = LLM_TOOLS
    req = urllib.request.Request(
        cfg['base'] + '/chat/completions',
        data=json.dumps(body, ensure_ascii=False).encode('utf-8'),
        headers={'Content-Type': 'application/json', 'Authorization': 'Bearer ' + cfg['key']})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.load(r)


def _parse_pseudo_calls(text):
    """兼容部分中转站返回的伪工具调用文本（DSML/XML 形式），解析出 工具名+参数"""
    s = str(text or '').replace('｜', '|')
    if 'DSML' not in s and 'invoke name=' not in s:
        return []
    out = []
    for m in re.finditer(r'(?is)<\|+\s*DSML\s*\|+\s*invoke\s+name="([^"]+)"(.*?)</\|+\s*DSML\s*\|+\s*invoke>', s):
        name, body = m.group(1), m.group(2)
        args = {}
        for pm in re.finditer(r'(?is)<\|+\s*DSML\s*\|+\s*parameter\s+name="([^"]+)"[^>]*>(.*?)</\|+\s*DSML\s*\|+\s*parameter>', body):
            k, v = pm.group(1), _html.unescape(pm.group(2)).strip()
            if k == 'fields' or k == 'approvers':
                try:
                    args[k] = json.loads(v)
                    continue
                except Exception:
                    pass
            args[k] = v
        out.append((name, args))
    return out


def llm_reply(content, pid, name, uid=''):
    """大模型回答；失败返回错误说明文本"""
    cfg = ai_cfg(pid)
    if not cfg:
        return None
    now = time.strftime('%Y-%m-%d %H:%M %A')
    sys_prompt = ('你是公司工作台的办公助手%s。当前时间 %s。'
                  '分工：涉及工作台数据（任务/日程/审批/抄送/@我/资料/统计）时先用工具查询再回答，禁止编造数据；'
                  '日报/周报/月报/工作总结用 work_report 工具取数后撰写，群消息统计用 msg_stats 工具；'
                  '报告定位为「与我相关的一切钉钉工作信息汇总」，报告开头注明统计区间与数据范围；'
                  '必须覆盖全部板块：一任务（含来源如钉钉多维表格、未完成与未来7天到期）、二审批（我发起/抄送我/我已处理分开列）、三日程、四钉钉群@我消息（按群汇总并附逐条明细）、五待我跟进（未回复消息/审批中）；'
                  '各板块下尽量列出明细条目而非只给数字，无数据的板块一句话带过即可；'
                  '通用问题（常识、天气、解释、写作、闲聊等）直接用你自己的知识回答，不要调工具。'
                  '可以执行的操作：建任务、完成任务、建日程、联网搜索(web_search)、读网页(fetch_url)、发邮件(send_email)、给同事发钉钉消息(send_dingtalk_msg)、发起OA审批(submit_approval)；'
                  '需要外部信息（政策/行情/技术/公司资料）时用 web_search 而不是凭记忆；'
                  '调用工具必须使用标准 function calling，一次只调用一个工具，禁止输出 XML/DSML 之类的标记文本；'
                  '涉及发邮件/发钉钉消息/提交审批这类有副作用的操作，先向用户确认收件人、标题、内容，再执行；工具返回缺配置或缺权限时如实告知用户缺什么；'
                  '报告类输出用 markdown 小标题+列表，正文简洁；'
                  '其他回复用中文、简洁（300 字内），列表最多 10 条，涉及金额可汇总。'
                  '查不到数据时直接说明。' % (('，服务对象：%s' % name if name else ''), now))
    messages = [{'role': 'system', 'content': sys_prompt},
                {'role': 'user', 'content': content}]
    try:
        for _ in range(3):
            j = _chat_once(cfg, messages, tools=True)
            msg = (j.get('choices') or [{}])[0].get('message') or {}
            calls = msg.get('tool_calls') or []
            if not calls:
                raw = str(msg.get('content') or '').strip()
                pseudo = _parse_pseudo_calls(raw)
                if pseudo:
                    lines = []
                    for pname, pargs in pseudo[:4]:
                        try:
                            pres = _tool_exec(pname, pargs, pid)
                        except Exception as e:
                            pres = json.dumps({'error': str(e)}, ensure_ascii=False)
                        lines.append('工具 %s 参数 %s 结果：%s' % (
                            pname, json.dumps(pargs, ensure_ascii=False), pres[:2500]))
                    messages.append({'role': 'assistant', 'content': raw})
                    messages.append({'role': 'user', 'content':
                                     '（系统：已按你的请求执行下列工具，请基于真实结果用中文回答用户最初的问题，'
                                     '不要输出 XML/DSML 标记）\n' + '\n'.join(lines)})
                    continue
                # 推理模型可能把正文放 reasoning_content，content 为空
                text = raw
                if not text:
                    text = str(msg.get('reasoning_content') or '').strip()
                    if text:
                        # 只取推理正文最后一段，避免输出长链思考过程
                        paras = [p.strip() for p in text.split('\n') if p.strip()]
                        text = paras[-1] if paras else ''
                return text or '模型没有返回内容（检查模型名或换一个模型试试）。'
            messages.append({'role': 'assistant', 'content': msg.get('content') or '',
                             'tool_calls': calls})
            for c in calls:
                fn = (c.get('function') or {})
                try:
                    argv = json.loads(fn.get('arguments') or '{}')
                except Exception:
                    argv = {}
                try:
                    res = _tool_exec(str(fn.get('name') or ''), argv, pid)
                except Exception as e:
                    res = json.dumps({'error': str(e)}, ensure_ascii=False)
                messages.append({'role': 'tool', 'tool_call_id': c.get('id') or '',
                                 'content': res[:3500]})
        j = _chat_once(cfg, messages, tools=False)
        msg = ((j.get('choices') or [{}])[0].get('message') or {})
        text = str(msg.get('content') or '').strip()
        if not text:
            text = str(msg.get('reasoning_content') or '').strip()
            paras = [p.strip() for p in text.split('\n') if p.strip()]
            text = paras[-1] if paras else ''
        return text or '模型没有返回内容（检查模型名或换一个模型试试）。'
    except Exception as e:
        return 'AI 接口调用失败：%s（在 AI 助手页「我的 AI 模型」检查地址 / 模型名 / Key）' % e


# ── 异步回复（sessionWebhook） ────────────────────────────────────────

def _webhook_reply(webhook, text):
    if not webhook:
        return
    try:
        body = {'msgtype': 'markdown', 'markdown': {'title': '工作台助手', 'text': text}}
        req = urllib.request.Request(webhook, data=json.dumps(body, ensure_ascii=False).encode('utf-8'),
                                     headers={'Content-Type': 'application/json'})
        urllib.request.urlopen(req, timeout=15).read()
    except Exception as e:
        print('[ai_bot] webhook 回复失败：%s' % e)


# ── 主入口 ────────────────────────────────────────────────────────────

def handle(body, content):
    """机器人回调处理。

    返回 HTTP 回调响应 dict（同步回复 / 空），返回 None 表示不处理
    （由 app.py 走原有「落等待回复表」逻辑）。
    """
    pid, name, uid = person_of(body)
    if not pid:
        _u = (_ding_conf().get('public_url') or '').rstrip('/')
        return _reply('#### 工作台 AI 助手\n\n你的钉钉账号还没有绑定工作台，'
                      '请先用钉钉扫码登录一次工作台' + ('（%s）' % _u if _u else '') + '再使用指令。')
    fast = fast_reply(content, pid, name)
    if fast is not None:
        return _reply(fast)
    # 日报/周报/月报/消息统计：交给模型，用 work_report / msg_stats 工具取真实数据
    if re.search(r'日报|周报|月报|工作总结|群消息|消息统计', content):
        content = ('请调用工具获取真实数据后整理输出，禁止编造数字。' + content)
    cfg = ai_cfg(pid)
    if cfg:
        webhook = str(body.get('sessionWebhook') or '')
        threading.Thread(target=_async_llm, args=(cfg, content, pid, name, webhook),
                         daemon=True).start()
        return {'msgtype': 'empty'}
    return None  # 未配置模型且不是指令 → 原有逻辑


def _async_llm(cfg, content, pid, name, webhook):
    try:
        text = llm_reply(content, pid, name)
        if text:
            _webhook_reply(webhook, text)
    except Exception as e:
        print('[ai_bot] 异步处理失败：%s' % e)
