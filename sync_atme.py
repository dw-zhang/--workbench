#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把钉钉「@我」消息同步到 wbdash 的「等待回复」

为什么在本地跑：钉钉不开放「@我消息列表」的服务端接口，只能由本人身份的
钉钉 CLI（dws）拉取；服务器上跑不了 dws，所以由本机拉取后推给工作台。

用法：
    python3 sync_atme.py                # 正常同步（默认回溯 180 天、自动翻页取全部）
    python3 sync_atme.py --dry-run      # 只看会写什么，不提交
    python3 sync_atme.py --days 30      # 只回溯最近 30 天
    python3 sync_atme.py --limit 200    # 每个会话多拉一些历史再判断是否已回复
    python3 sync_atme.py --tables       # 强制重扫钉钉表格（AI 表格）里的评论@我

判定「未回复（等待回复）」：该 @我 消息之后，同一会话里没有我自己发的消息。
判定「已回复」：该 @我 消息之后，同一会话里出现过我自己发的消息。
（只是启发式，判错可在页面上手动改状态；同步不会把已回复改回等待回复）
默认全部保留（含欢迎语、系统通知）；加 --skip-noise 才跳过。

表格内容的三类来源（都会打标签进「等待回复」）：
  1. 表格截图：@我 消息里带图片（ERP 单据截图为主）→ 备注「表格截图」
  2. 表格/文件分享：群里发 .xlsx / 钉钉表格并 @我 → 备注「表格/文件分享」
  3. 表格内评论：AI 表格（智能表格）记录评论里提到我 → 备注「表格内@我」，
     每 20 小时全量扫一遍（4 个 Base 约 400 条记录、6 分钟），有新评论才写
"""
import argparse
import json
import os
import subprocess
import sys
import time
import urllib.parse
import urllib.request

HOST = os.environ.get('WBDASH_HOST', 'http://127.0.0.1:8787')
PASSWORD = os.environ.get('WBDASH_PASSWORD', '')
ME = os.environ.get('WBDASH_ME', '')
WAIT_DB = 'DiQ0qiGK3ZejC7g3bc1NDk'
TABLE_TS_FILE = os.path.expanduser('~/.wbdash_table_at.json')
TABLE_GAP = 20 * 3600          # 表格评论扫描间隔（秒）
# 欢迎语/寒暄类关键词：命中即跳过，不进「等待回复」
NOISE_WORDS = ('欢迎加入', '送花花', '送朵小花', '欢迎新人', '让我们一起欢迎')
# 表格/文件分享类关键词
SHARE_WORDS = ('表格', 'xlsx', 'xls', 'csv', '在线文档', '钉钉文档', '多维表', '智能表格')


def api(path, body=None, token=None, method=None):
    url = HOST + path
    data = json.dumps(body, ensure_ascii=False).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method or ('POST' if data else 'GET'))
    req.add_header('Content-Type', 'application/json')
    if token:
        req.add_header('Authorization', 'Bearer ' + token)
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read().decode())


def dws(args, timeout=180):
    p = subprocess.run(['dws'] + args, capture_output=True, text=True, timeout=timeout)
    if p.returncode != 0:
        raise RuntimeError((p.stderr or p.stdout or '').strip()[:300])
    return json.loads(p.stdout or '{}')


def clean_text(t):
    t = str(t or '')
    for kind in ('图片消息', '视频消息', '语音消息', '文件消息', '表情消息'):
        while True:
            i = t.find('[' + kind + '](')
            if i < 0:
                break
            j = t.find(')', i)
            if j < 0:
                break
            label = '[图片]' if kind == '图片消息' else '[' + kind.replace('消息', '') + ']'
            t = t[:i] + label + t[j + 1:]
    t = t.replace('@' + ME, '').replace('\u2005', ' ')
    return ' '.join(t.split()).strip()


def tag_of(text):
    """给 @我 消息打内容类型标签（主要为了把表格类内容标出来）"""
    if '图片消息' in str(text or ''):
        return '表格截图'
    return ''


def conv_link(cid):
    return ('dingtalk://dingtalkclient/action/opendiscussion?conversationId='
            + urllib.parse.quote(cid or '', safe=''))


def times_of(msgs):
    out = []
    for m in msgs or []:
        out.append((str(m.get('createTime') or ''), str(m.get('sender') or '')))
    out.sort()
    return out


def replied_after(timeline, when):
    """该时间点之后有没有我自己发的消息"""
    for t, s in timeline:
        if t > when and s == ME:
            return True
    return False


# ── 表格/文件分享类 @我（从群消息时间线里补抓，+at-me 只回纯文本） ──
def is_share_at(msg):
    if str(msg.get('sender') or '') == ME:
        return False
    txt = str(msg.get('text') or '')
    if ('@' + ME) not in txt and ('@' + ME) not in txt.replace('\u2005', ' '):
        return False
    has_file = False
    for r in (msg.get('resourceRefs') or []):
        if isinstance(r, dict) and str(r.get('contentType') or '') == 'file':
            has_file = True
    return has_file or any(k in txt for k in SHARE_WORDS)


def fetch_groups_at(at_items):
    """每个出现过的会话拉最近消息（含卡片/文件类），返回 cid → [消息dict]"""
    groups = {}
    for it in at_items:
        cid = it.get('conversationId') or (it.get('conversation') or {}).get('openConversationId')
        name = (it.get('conversation') or {}).get('name') or ''
        if cid and cid not in groups:
            groups[cid] = name
    out = {}
    for cid, name in groups.items():
        try:
            got = dws(['chat', '+chat-messages', '--group', name or cid,
                       '--limit', '200', '--format', 'json'])
            out[cid] = got.get('messages') or []
        except Exception as e:
            print('  群消息读取失败（%s）：%s' % (name[:20], str(e)[:80]))
            out[cid] = []
    return out


# ── 钉钉表格（AI 表格）评论里的 @我 ─────────────────────────
def scan_table_at(days=30):
    """全量扫各 Base 各表的记录评论，命中 @我 的返回等待回复条目"""
    try:
        gl = dws(['aitable', 'base', 'list', '--format', 'json'])
    except Exception as e:
        print('  表格 Base 列表失败：%s' % str(e)[:100])
        return []
    out = []
    for b in ((gl.get('data') or {}).get('bases') or []):
        bid, bname = b.get('baseId'), (b.get('baseName') or '')
        try:
            d = dws(['aitable', 'table', 'get', '--base-id', bid, '--format', 'json'])
        except Exception:
            continue
        for t in ((d.get('data') or {}).get('tables') or []):
            tid, tname = t.get('tableId'), (t.get('tableName') or '')
            recs, cursor = [], None
            for _ in range(60):
                args = ['aitable', 'record', 'query', '--base-id', bid, '--table-id', tid,
                        '--limit', '100', '--format', 'json']
                if cursor:
                    args += ['--cursor', cursor]
                try:
                    q = dws(args)
                except Exception:
                    break
                if q.get('error') or q.get('err'):
                    break
                recs += (q.get('data') or {}).get('records') or []
                cursor = (q.get('data') or {}).get('nextCursor')
                if not cursor:
                    break
            for r in recs:
                rid = r.get('recordId') or r.get('id')
                if not rid:
                    continue
                try:
                    c = dws(['aitable', 'comment', 'list', '--base-id', bid,
                             '--table-id', tid, '--record-id', rid,
                             '--limit', '50', '--format', 'json'])
                except Exception:
                    continue
                for x in ((c.get('data') or {}).get('comments')
                          or c.get('comments') or []):
                    body = json.dumps(x, ensure_ascii=False)
                    if ME not in body:
                        continue
                    if isinstance(x, dict):
                        text = (str(x.get('content') or x.get('text') or '')
                                or body[:200])
                        author = str(x.get('creator') or x.get('creatorName')
                                     or x.get('userName') or x.get('author') or '')
                        when = str(x.get('createTime') or x.get('createdAt')
                                   or x.get('gmtCreate') or '')
                        cmid = str(x.get('commentId') or x.get('id') or '')
                    else:
                        text, author, when, cmid = body[:200], '', '', ''
                    text = ' '.join(str(text).split())[:180]
                    out.append({
                        '事项名称': text or ('%s/%s 里有新评论' % (bname, tname)),
                        '来源': ('钉钉表格 · %s/%s' % (bname, tname))[:60],
                        '发起时间': (when.replace(' ', 'T') + '+08:00') if when else '',
                        '状态': '等待回复',
                        '备注': '表格内@我',
                        '处理链接': 'https://alidocs.dingtalk.com/i/nodes/%s' % bid,
                        '消息ID': 'atbl:%s:%s:%s:%s' % (bid[:8], tid[:8], rid[:8], cmid[:12]),
                        '群名称': '%s / %s' % (bname, tname),
                        '发送人': author,
                    })
    return out


def main():
    global HOST
    ap = argparse.ArgumentParser()
    ap.add_argument('--dry-run', action='store_true')
    ap.add_argument('--days', type=int, default=180, help='回溯天数（默认 180）')
    ap.add_argument('--skip-noise', action='store_true',
                    help='跳过系统通知与欢迎语（默认全部保留）')
    ap.add_argument('--limit', type=int, default=200)
    ap.add_argument('--host', default=HOST)
    ap.add_argument('--password', default=PASSWORD)
    ap.add_argument('--tables', action='store_true', help='强制重扫表格评论@我')
    a = ap.parse_args()

    HOST = a.host
    if not a.password:
        print('缺少管理员密码：请设置环境变量 WBDASH_PASSWORD，或加 --password 参数')
        sys.exit(2)
    token = api('/auth/admin', {'password': a.password})['token']

    at = dws(['chat', '+at-me', '--days', str(a.days), '--page-all',
              '--max-items', '2000', '--format', 'json'])
    items = at.get('items') or []
    total = len(items)
    if a.skip_noise:
        # 可选过滤：AI小钉、发送人=群名的机器人通知、欢迎语寒暄
        real = []
        for it in items:
            conv = (it.get('conversation') or {}).get('name') or ''
            sender = str(it.get('sender') or '')
            if sender == 'AI小钉' or (conv and sender == conv):
                continue
            if any(k in clean_text(it.get('text')) for k in NOISE_WORDS):
                continue
            real.append(it)
        items = real
    print('@我 消息 %d 条（回溯 %d 天%s）'
          % (len(items), a.days, '，跳过系统通知/欢迎语 %d 条' % (total - len(items)) if total != len(items) else ''))
    if not items:
        print('没有需要同步的 @我 消息')
        return 0

    # 逐会话拉消息：① 判断我是否已经回过 ② 补抓表格/文件分享类 @我
    timelines = {}
    for it in items:
        cid = it.get('conversationId') or (it.get('conversation') or {}).get('openConversationId')
        if not cid or cid in timelines:
            continue
        name = (it.get('conversation') or {}).get('name') or ''
        # 群名不唯一 / 是单聊时 --group 会解析失败（resolution_not_found），
        # 用 openConversationId 精确路由再试一次，避免整段会话时间线拿不到。
        tries = [['--group', name or cid], ['--conversation-id', cid]]
        got = None
        err = None
        for tgt in tries:
            try:
                got = dws(['chat', '+chat-messages'] + tgt +
                          ['--limit', str(a.limit), '--format', 'json'])
                break
            except Exception as e:
                err = e
        if got is not None:
            timelines[cid] = got.get('messages') or []
        else:
            print('  群消息读取失败（%s）：%s' % (name, str(err)[:80]))
            timelines[cid] = []
    timelines_only = {cid: times_of(ms) for cid, ms in timelines.items()}

    existed = api('/api/query?db=%s&size=2000' % WAIT_DB, token=token).get('results') or []
    by_mid, by_key = {}, {}
    for r in existed:
        m = str(r.get('消息ID') or '').strip()
        if m:
            by_mid[m] = r
        by_key[(str(r.get('事项名称') or '').strip(), str(r.get('发起时间') or '')[:16])] = r

    payload, new_n, upd_n, open_n = [], 0, 0, 0
    seen_mids = set()
    tag_n = {'表格截图': 0, '表格/文件分享': 0, '表格内@我': 0}

    def add(rec, replied=None):
        nonlocal new_n, upd_n, open_n
        mid = rec.get('消息ID') or ''
        if mid and mid in seen_mids:
            return
        if mid:
            seen_mids.add(mid)
        hit = by_mid.get(mid) if mid else None
        if not hit:
            hit = by_key.get((rec['事项名称'], rec['发起时间'][:16]))
        if hit:
            hs = str(hit.get('状态') or '')
            if replied and hs != '已关闭':
                rec['状态'] = '已回复'
            elif not replied and hs in ('已回复', '已关闭'):
                # 手动标记已回复/已关闭的，即使启发式没在钉钉里检测到我的回复，也不刷回「等待回复」
                rec['状态'] = hs
            upd_n += 1
        else:
            if replied is False:
                open_n += 1
            new_n += 1
        payload.append(rec)

    for it in items:
        cid = it.get('conversationId') or (it.get('conversation') or {}).get('openConversationId')
        conv = (it.get('conversation') or {}).get('name') or ''
        when = str(it.get('time') or '')
        if not when:
            continue
        raw = str(it.get('text') or '')
        text = clean_text(raw) or '（无内容）'
        tag = tag_of(raw)
        if tag:
            tag_n[tag] += 1
        replied = replied_after(timelines_only.get(cid) or [], when)
        rec = {
            '事项名称': text,
            '来源': (conv + ' · ' if conv else '') + str(it.get('sender') or ''),
            '发起时间': when.replace(' ', 'T') + '+08:00',
            '备注': '钉钉群@我消息' + ((' · ' + tag) if tag else ''),
            '处理链接': conv_link(cid),
            '消息ID': str(it.get('messageId') or ''),
            '群名称': conv,
            '发送人': str(it.get('sender') or ''),
        }
        rec['状态'] = '已回复' if replied else '等待回复'
        add(rec, replied)

    # 表格/文件分享类 @我：+at-me 不返回卡片/文件消息，从群时间线补抓
    share_n = 0
    for cid, msgs in timelines.items():
        conv = ''
        for it in items:
            c2 = it.get('conversationId') or (it.get('conversation') or {}).get('openConversationId')
            if c2 == cid:
                conv = (it.get('conversation') or {}).get('name') or ''
                break
        for m in msgs:
            if not is_share_at(m):
                continue
            when = str(m.get('createTime') or '')
            if not when:
                continue
            text = clean_text(m.get('text')) or '（无内容）'
            mid = 'share:' + str(m.get('messageId') or when)
            replied = replied_after(timelines_only.get(cid) or [], when)
            files = [str(r.get('name') or '') for r in (m.get('resourceRefs') or [])
                     if isinstance(r, dict) and str(r.get('contentType') or '') == 'file']
            note = '钉钉群@我消息 · 表格/文件分享'
            if files:
                note += '（%s）' % '、'.join(f for f in files if f)[:60]
            rec = {
                '事项名称': text[:200],
                '来源': (conv + ' · ' if conv else '') + str(m.get('sender') or ''),
                '发起时间': when.replace(' ', 'T') + '+08:00',
                '状态': '已回复' if replied else '等待回复',
                '备注': note,
                '处理链接': conv_link(cid),
                '消息ID': mid,
                '群名称': conv,
                '发送人': str(m.get('sender') or ''),
            }
            tag_n['表格/文件分享'] += 1
            share_n += 1
            add(rec, replied)

    # 钉钉表格（AI 表格）评论里的 @我：20 小时一次，--tables 强制
    tbl_n = 0
    last_ts = 0
    try:
        last_ts = float(json.load(open(TABLE_TS_FILE)).get('ts', 0))
    except Exception:
        pass
    if a.tables or time.time() - last_ts > TABLE_GAP:
        print('扫描钉钉表格评论 @我（%s）…'
              % ('强制' if a.tables else '距上次超过 20 小时'))
        try:
            hits = scan_table_at()
        except Exception as e:
            hits = []
            print('  表格扫描失败：%s' % str(e)[:120])
        for rec in hits:
            tag_n['表格内@我'] += 1
            tbl_n += 1
            add(rec, replied=None)
        try:
            json.dump({'ts': time.time()}, open(TABLE_TS_FILE, 'w'))
        except Exception:
            pass
        print('表格评论 @我：%d 条' % tbl_n)
    else:
        print('表格评论扫描：距上次不足 20 小时，跳过（--tables 可强制）')

    print('@我 消息 %d 条：新增 %d / 更新 %d（其中仍未回复 %d）'
          % (len(items), new_n, upd_n, open_n))
    print('分类统计：' + ' · '.join('%s %d' % (k, v) for k, v in tag_n.items()))
    for rec in payload[:12]:
        print('  -', rec['发起时间'][:16], '|', rec.get('状态', '(不变)'), '|',
              rec['备注'].replace('钉钉群@我消息', '群@')[:24], '|',
              rec['事项名称'][:36], '|', rec['来源'][:24])
    if a.dry_run:
        print('dry-run：未提交')
        return 0
    stat = api('/api/wait/upsert', {'items': payload}, token=token)
    print('写入结果：', stat)
    return 0


if __name__ == '__main__':
    sys.exit(main())
