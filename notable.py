# -*- coding: utf-8 -*-
"""钉钉多维表格（AI 表格 / Notable）→ 工作台任务表（服务器端，官方 API）

复用钉钉应用的 app_key/app_secret 换 token；operatorId 用管理员的
unionId（accounts.extra 里存着，通讯录同步时写入）。

配置（管理员界面「钉钉多维表格」平台）：
  bases       Base 链接或 baseId，逗号/换行分隔。
              链接形如 https://alidocs.dingtalk.com/i/nodes/<baseId>?... 自动提取
  notify_open 新任务发钉钉工作通知（填 0 关闭；默认开）。
              每个 Base 首次全量导入静默，之后的增量新任务才通知。

行为
- 枚举 Base 下所有数据表，自动识别任务字段（标题/状态/优先级/截止/执行人）
- 只同步「执行人/负责人」类成员字段非空的记录，按成员逐人落库
  （同一行多个人 → 每人一条，与审批同人逻辑一致）
- 去重键：钉钉任务ID = aitbl:<recordId>（与 sync_aitable_task.py 同键，不冲突）
- 成员匹配：unionId / userId → accounts 表；匹配不到的人跳过并计数
- 一个 Base 里没有任何成员字段的数据表 → 跳过（不是任务表）
"""
import datetime
import json
import time
import urllib.parse
import urllib.request

import db
import notify
import settings

API = 'https://api.dingtalk.com/v1.0/notable/bases/%s'

TITLE_NAMES = ('任务描述', '任务名称', '任务内容', '标题', '事项', '名称', '任务',
               '工作内容', '问题描述', '风险内容', '会议名称', '文件名称', '功能指标')
STATUS_NAMES = ('状态', '任务状态', '进展状态', '当前状态', '是否关闭')
PRIORITY_NAMES = ('优先级', '任务优先级', '紧急程度', '风险程度', '程度')
DUE_NAMES = ('预计完成日期', '截止时间', '截止日期', '计划完成时间', '要求完成日期', '到期日',
             '变更后计划完成时间', '会议日期', '实际完成时间')
USER_HINTS = ('执行人', '负责人', '责任人', '处理人', '指派给', '任务执行人', '跟进人',
              '任务负责人', '解决责任人', '提出者', '会议主持人')

STATUS_MAP = {
    '已完成': '已完成', '完成': '已完成', '已关闭': '已完成', 'done': '已完成',
    '进行中': '进行中', '处理中': '进行中', 'doing': '进行中', '进行': '进行中',
    '未开始': '待处理', '待处理': '待处理', '待启动': '待处理', '待办': '待处理', 'todo': '待处理',
}
PRIORITY_MAP = {
    '重要紧急': '高', '紧急重要': '高', '最高': '高', '高': '高', 'P0': '高', 'P1': '高',
    '紧急不重要': '中', '重要不紧急': '中', '中': '中', '普通': '中', 'P2': '中',
    '不重要不紧急': '低', '低': '低', 'P3': '低',
}


class NotableError(Exception):
    pass


# ── HTTP ──────────────────────────────────────────────────────────────
def _req(url, tok, body=None, method=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method or ('POST' if data else 'GET'))
    req.add_header('x-acs-dingtalk-access-token', tok)
    if data:
        req.add_header('Content-Type', 'application/json')
    last = None
    for i in range(3):  # 限流/抖动退避
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            raw = e.read().decode('utf-8', 'replace')
            try:
                err = json.loads(raw)
                code, msg = err.get('code', ''), err.get('message', raw[:120])
            except Exception:
                code, msg = '', raw[:160]
            if e.code in (429, 500, 502, 503) and i < 2:
                time.sleep(2 * (i + 1))
                continue
            raise NotableError('HTTP %s %s %s' % (e.code, code, msg))
        except Exception as e:
            last = e
            time.sleep(1)
    raise NotableError(str(last))


def _token():
    ak = settings.g('dingtalk:app_key') or settings.g('app_key')
    sk = settings.g('dingtalk:app_secret') or settings.g('app_secret')
    if not ak or not sk:
        raise NotableError('钉钉应用未配置（app_key/app_secret）')
    q = urllib.parse.urlencode({'appkey': ak, 'appsecret': sk})
    d = json.loads(urllib.request.urlopen(
        'https://oapi.dingtalk.com/gettoken?' + q, timeout=15).read().decode())
    return d['access_token']


def _operator_id():
    """管理员的 unionId（accounts.extra），整个运行期缓存"""
    global _OP
    if _OP:
        return _OP
    admin = settings.g('dingtalk:admin_userid') or settings.g('dingtalk:user_id') or settings.g('user_id')
    c = db.connect()
    with db._lock:
        row = c.execute('SELECT extra FROM accounts WHERE platform=? AND account_id=?',
                        ('dingtalk', admin)).fetchone() if admin else None
        if not row:
            row = c.execute("SELECT extra FROM accounts WHERE platform='dingtalk' AND extra LIKE '%unionid%' LIMIT 1").fetchone()
    if row:
        try:
            _OP = json.loads(row[0] or '{}').get('unionid', '')
        except Exception:
            _OP = ''
    if not _OP:
        raise NotableError('找不到管理员 unionId（accounts.extra）')
    return _OP


_OP = ''


# ── 值解析 ────────────────────────────────────────────────────────────
def _cell_text(v):
    if v is None:
        return ''
    if isinstance(v, str):
        return v.strip()
    if isinstance(v, (int, float)):
        return str(v)
    if isinstance(v, dict):
        for k in ('name', 'text', 'value', 'title'):
            if v.get(k) not in (None, ''):
                return _cell_text(v[k])
        return ''
    if isinstance(v, list):
        return '、'.join(x for x in (_cell_text(i) for i in v) if x)
    return str(v)


def _cell_date(v):
    """date 字段：毫秒时间戳或日期串 → 'YYYY-MM-DD'"""
    if isinstance(v, (int, float)) or (isinstance(v, str) and v.strip().isdigit()):
        try:
            return datetime.date.fromtimestamp(int(v) / 1000).isoformat()
        except Exception:
            return ''
    s = _cell_text(v)
    return s[:10] if len(s) >= 10 and s[4] == '-' else ''


def _cell_members(v):
    """成员字段值 → [(unionid/userid, 姓名)]"""
    out = []
    for m in v if isinstance(v, list) else ([v] if isinstance(v, dict) else []):
        if not isinstance(m, dict):
            continue
        uid = str(m.get('unionId') or m.get('userId') or m.get('id') or '').strip()
        nm = str(m.get('nickName') or m.get('name') or m.get('userName') or '').strip()
        if uid or nm:
            out.append((uid, nm))
    return out


def _pick(fields, names):
    """按名字提示挑字段（fields 为该表出现过的字段名集合）"""
    for n in names:
        if n in fields:
            return n
    return ''


# ── 人员映射 ──────────────────────────────────────────────────────────
def _person_map():
    """(unionid/userid) → person_id；顺带返回 userid→姓名"""
    u2p, i2n = {}, {}
    c = db.connect()
    with db._lock:
        rows = c.execute("SELECT person_id, account_id, extra FROM accounts WHERE platform='dingtalk'").fetchall()
    for pid, aid, extra in rows:
        if aid:
            u2p[str(aid)] = pid
        try:
            d = json.loads(extra or '{}')
        except Exception:
            continue
        if d.get('unionid'):
            u2p[d['unionid']] = pid
        if d.get('name') or d.get('nick'):
            i2n[str(aid)] = d.get('name') or d.get('nick')
    return u2p, i2n


# ── 核心 ──────────────────────────────────────────────────────────────
def _base_ids(raw):
    import re as _re
    out = []
    for part in _re.split(r'[\s,;，；]+', str(raw or '')):
        part = part.strip()
        if not part:
            continue
        m = _re.search(r'/i/nodes/([A-Za-z0-9]+)', part)
        if m:
            out.append(m.group(1))
        elif _re.fullmatch(r'[A-Za-z0-9]{16,}', part):
            out.append(part)
    return out


def _list_sheets(tok, bid):
    d = _req(API % bid + '/sheets?operatorId=' + urllib.parse.quote(_operator_id()), tok)
    for key in ('sheets', 'value', 'result', 'data'):
        v = d.get(key)
        if isinstance(v, list) and v:
            return v
        if isinstance(v, dict):
            for k2 in ('sheets', 'items', 'list'):
                if isinstance(v.get(k2), list):
                    return v[k2]
    return []


def _iter_records(tok, bid, sheet):
    """sheet 可以是 dict（含 id/name/sheetId）或字符串 id"""
    if isinstance(sheet, dict):
        sid = sheet.get('id') or sheet.get('sheetId') or sheet.get('sheetIdOrName') or ''
        sname = sheet.get('name') or sheet.get('sheetName') or ''
    else:
        sid, sname = str(sheet), ''
    url = (API % bid + '/sheets/%s/records/list?operatorId=%s'
           % (urllib.parse.quote(sid), urllib.parse.quote(_operator_id())))
    recs, token = [], None
    for _ in range(100):
        body = {'maxResults': 100}
        if token:
            body['nextToken'] = token
        d = _req(url, tok, body)
        recs += d.get('records') or d.get('value') or []
        token = d.get('nextToken') or d.get('nextCursor') or ''
        if not d.get('hasMore', bool(token)) or not token:
            break
    return sname, recs


def create_record(tok, bid, sid, fields):
    """往数据表写一行任务。fields 值类型：
    文本/单选 → 字符串；成员 → [{'unionId': ..}]；日期 → 毫秒时间戳。
    需要「AI 表格应用写权限」；返回新记录 id（失败抛 NotableError）。"""
    url = (API % bid + '/sheets/%s/records?operatorId=%s'
           % (urllib.parse.quote(sid), urllib.parse.quote(_operator_id())))
    d = _req(url, tok, {'records': [{'fields': fields}]})
    recs = d.get('records') or d.get('value') or []
    r0 = recs[0] if recs else {}
    return str(r0.get('recordId') or r0.get('id') or '')


def delete_record(tok, bid, sid, rid):
    """删除一行（撤回误写/清理测试数据）。实测正确端点：
    POST /sheets/{sid}/records/delete，body {"recordIds": [..]}"""
    url = (API % bid + '/sheets/%s/records/delete?operatorId=%s'
           % (urllib.parse.quote(sid), urllib.parse.quote(_operator_id())))
    _req(url, tok, {'recordIds': [str(rid)]})


def sync_notable(cfg, say):
    pc = settings.get_platform('notable')
    if not settings.platform_ready('notable'):
        return None
    bases = _base_ids(pc.get('bases'))
    if not bases:
        return None
    notify_open = str(pc.get('notify_open') or '1') != '0'
    imported = [x for x in (settings.g('notable:imported') or '').split(',') if x]

    tok = _token()
    u2p, i2n = _person_map()
    stat = {'new': 0, 'upd': 0, 'skip': 0, 'unmatched': 0, 'err': []}

    for bid in bases:
        first = bid not in imported
        try:
            sheets = _list_sheets(tok, bid)
        except NotableError as e:
            stat['err'].append('Base %s：%s' % (bid[:8], e))
            continue
        if not sheets:
            stat['err'].append('Base %s：没有可读的数据表（检查 Base 权限）' % bid[:8])
            continue
        say('多维表格：Base %s… 共 %d 张数据表' % (bid[:8], len(sheets)))
        for sheet in sheets:
            sid0 = str(sheet.get('id') or sheet.get('sheetId') or '') if isinstance(sheet, dict) else str(sheet)
            try:
                bound = db.project_by_sheet(bid, sid0)
            except Exception:
                bound = None
            if bound:
                say('  %s：已绑定项目「%s」，由项目同步接管' % (sheet.get('name') or sid0[:8],
                                                              bound.get('name', '')))
                continue
            try:
                sname, recs = _iter_records(tok, bid, sheet)
            except NotableError as e:
                stat['err'].append('数据表读取：%s' % e)
                continue
            if not recs:
                continue
            # 字段名集合 + 找任务字段
            names = set()
            for r in recs:
                names.update((r.get('fields') or {}).keys())
            f_title = _pick(names, TITLE_NAMES)
            f_status = _pick(names, STATUS_NAMES)
            f_pri = _pick(names, PRIORITY_NAMES)
            f_due = _pick(names, DUE_NAMES)
            f_user = _pick(names, USER_HINTS)
            # 名字没命中时兜底：值形态是成员列表的字段
            if not f_user:
                for n in sorted(names):
                    v = recs[0].get('fields', {}).get(n)
                    if isinstance(v, list) and v and isinstance(v[0], dict) and \
                       (v[0].get('unionId') or v[0].get('userId') or v[0].get('nickName')):
                        f_user = n
                        break
            if not f_user or not f_title:
                say('  %s：不是任务表（缺执行人/标题字段），跳过' % (sname or '?'))
                continue

            burl = 'https://alidocs.dingtalk.com/i/nodes/%s' % bid
            new = upd = skip = 0
            for r in recs:
                f = r.get('fields') or {}
                rid = str(r.get('recordId') or r.get('id') or '')
                members = _cell_members(f.get(f_user))
                if not members:
                    skip += 1
                    continue
                title = ' '.join(_cell_text(f.get(f_title)).split())[:120]
                if not title:
                    skip += 1
                    continue
                status = STATUS_MAP.get(_cell_text(f.get(f_status)), _cell_text(f.get(f_status)) or '待处理')
                pri = PRIORITY_MAP.get(_cell_text(f.get(f_pri)), '中')
                due = _cell_date(f.get(f_due))
                owners = [nm for _, nm in members if nm]
                props = {
                    '任务名称': title, '优先级': pri, '状态': status,
                    '截止时间': due, '负责人': '、'.join(owners)[:40],
                    '所属项目': ('多维表格 · %s' % (sname or bid[:8]))[:40],
                    '处理链接': burl, '来源应用': '钉钉多维表格',
                    '钉钉任务ID': 'aitbl:%s' % rid,
                }
                for uid, nm in members:
                    pid = u2p.get(uid)
                    if not pid:
                        stat['unmatched'] += 1
                        continue
                    exist = db.find_record(db.TABLES['task'], '钉钉任务ID', props['钉钉任务ID'], pid)
                    if exist:
                        db.update_record(db.TABLES['task'], exist, props, person_id=pid)
                        upd += 1
                    else:
                        db.add_record(db.TABLES['task'], props, source='notable', person_id=pid)
                        new += 1
                        if notify_open and not first and status != '已完成':
                            nm2 = i2n.get(uid) or nm
                            notify.push(pid, 'todo', '新任务：%s' % title,
                                        burl, extra='多维表格')
            stat['new'] += new
            stat['upd'] += upd
            stat['skip'] += skip
            say('  %s：记录 %d，新 +%d / 更 %d / 空执行人 %d'
                % (sname or bid[:8], len(recs), new, upd, skip))
        if first:
            imported.append(bid)
    if stat['new'] or stat['upd']:
        db.cfg_set({'notable:imported': ','.join(imported)})
    elif imported:
        db.cfg_set({'notable:imported': ','.join(imported)})
    if stat['unmatched']:
        stat['err'].append('有 %d 条成员匹配不到工作台人员（跳过）' % stat['unmatched'])
    return stat
