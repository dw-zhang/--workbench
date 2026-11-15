# -*- coding: utf-8 -*-
"""SQLite 存储层：表结构(schema) + 记录(records) + 人员(persons/accounts)
+ 会话(sessions) + 配置(config) + 同步日志(sync_log)

设计要点
- 记录体统一存 JSON，字段与前端页面完全一致（前端不用改）
- records.person_id：每条数据属于谁（员工登录后按它过滤）
- records.seq 单调递增，用于稳定的默认排序（越新越靠前）
- 所有写入走事务，避免并发写坏库
"""
import json
import os
import sqlite3
import threading
import time
import uuid

HERE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.environ.get('WB_DB', os.path.join(HERE, 'data', 'workbench.db'))

# 8 张表的标识：直接用语义名做 records.database 的值
# （老版本用过外部平台的 18 位 databaseId 作标识，已统一迁移为语义名；
#   如需对接外部表 ID，改这里即可，前端只认语义名。）
TABLES = {
    'task':  'task',
    'sched': 'sched',
    'wait':  'wait',
    'files': 'files',
    'ding':  'ding',
    'meet':  'meet',
    'cat':   'cat',
    'apps':  'apps',
}
DB2KEY = {v: k for k, v in TABLES.items()}

# 公共数据：不按人过滤
# - 分类(cat)、应用配置(apps)：无个人隐私的元数据
# - 资料库(files)：按人隔离，可见范围 = 各人钉钉权限（同步时用本人 token 枚举，无权的不入库）
# - 会议(meet)：按人隔离，只显示本人组织/参与的会议，避免泄露其他部门会议主题与参会名单
# 管理员（密码登录、不过滤）仍可看全量。
PUBLIC_TABLES = {TABLES['cat'], TABLES['apps']}

PLATFORMS = ('dingtalk', 'feishu', 'github', 'kingdee', 'wecom')

_lock = threading.RLock()
_conn = None


def connect():
    global _conn
    with _lock:
        if _conn is None:
            os.makedirs(os.path.dirname(DB_PATH) or '.', exist_ok=True)
            _conn = sqlite3.connect(DB_PATH, check_same_thread=False)
            _conn.row_factory = sqlite3.Row
            _conn.execute('PRAGMA journal_mode=WAL')
            _conn.execute('PRAGMA synchronous=NORMAL')
            _init(_conn)
        return _conn


def _init(c):
    c.executescript("""
    CREATE TABLE IF NOT EXISTS schemas (
      database   TEXT PRIMARY KEY,
      title      TEXT,
      properties TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS records (
      id         TEXT PRIMARY KEY,
      database   TEXT NOT NULL,
      data       TEXT NOT NULL,
      seq        INTEGER,
      source     TEXT,
      updated_at TEXT
    );
    CREATE INDEX IF NOT EXISTS idx_records_db  ON records(database, seq);
    CREATE INDEX IF NOT EXISTS idx_records_src ON records(database, source);
    CREATE TABLE IF NOT EXISTS config (
      k TEXT PRIMARY KEY,
      v TEXT
    );
    CREATE TABLE IF NOT EXISTS sync_log (
      id   INTEGER PRIMARY KEY AUTOINCREMENT,
      at   TEXT,
      kind TEXT,
      ok   INTEGER,
      rows INTEGER,
      msg  TEXT
    );
    CREATE TABLE IF NOT EXISTS persons (
      person_id  TEXT PRIMARY KEY,
      name       TEXT,
      status     TEXT DEFAULT 'bound',
      match_by   TEXT,
      phones     TEXT,
      emails     TEXT,
      source     TEXT,
      created_at TEXT,
      updated_at TEXT
    );
    CREATE TABLE IF NOT EXISTS accounts (
      person_id  TEXT NOT NULL,
      platform   TEXT NOT NULL,
      account_id TEXT NOT NULL,
      name       TEXT,
      extra      TEXT,
      PRIMARY KEY (person_id, platform, account_id)
    );
    CREATE INDEX IF NOT EXISTS idx_accounts_acc ON accounts(platform, account_id);
    CREATE TABLE IF NOT EXISTS sessions (
      token      TEXT PRIMARY KEY,
      kind       TEXT,
      person_id  TEXT,
      name       TEXT,
      created_at TEXT,
      expires    TEXT
    );
    CREATE TABLE IF NOT EXISTS judge_log (
      id         TEXT PRIMARY KEY,
      pid        TEXT,
      person_id  TEXT,
      title      TEXT,
      typ        TEXT,
      verdict    TEXT,
      confidence TEXT,
      payload    TEXT,
      adopted    INTEGER,
      model      TEXT,
      cost_ms    INTEGER,
      created_at TEXT,
      fb_at      TEXT
    );
    CREATE INDEX IF NOT EXISTS idx_judge_pid ON judge_log(pid);
    CREATE INDEX IF NOT EXISTS idx_judge_time ON judge_log(created_at);
    """)
    # 兼容老库：records 增加 person_id
    cols = [r[1] for r in c.execute('PRAGMA table_info(records)').fetchall()]
    if 'person_id' not in cols:
        c.execute('ALTER TABLE records ADD COLUMN person_id TEXT')
    c.execute('CREATE INDEX IF NOT EXISTS idx_records_person ON records(database, person_id)')
    # 兼容老库：persons 增加 last_login（同步范围用：登录过的员工才纳入同步）
    cols_p = [r[1] for r in c.execute('PRAGMA table_info(persons)').fetchall()]
    if 'last_login' not in cols_p:
        c.execute('ALTER TABLE persons ADD COLUMN last_login TEXT')
    _migrate_legacy_tables(c)
    c.commit()


def _migrate_legacy_tables(c):
    """老库迁移：records/schemas 的 database 曾用外部平台的 18 位表 ID，
    现在统一用语义名（task/sched/wait/files/ding/meet/cat/apps）。

    迁移依据 schemas.title（TASK/SCHED/…，与本程序的表名一一对应），
    因此代码里不需要保留任何外部 ID。幂等：已迁移过的库不会重复执行。
    """
    known = {k.upper(): k for k in TABLES}
    rows = c.execute('SELECT database,title,properties FROM schemas').fetchall()
    for db_id, title, props in rows:
        key = known.get(str(title or '').strip().upper())
        if not key or key == db_id:
            continue
        n = c.execute('SELECT COUNT(*) FROM records WHERE database=?', (db_id,)).fetchone()[0]
        c.execute('UPDATE records SET database=? WHERE database=?', (key, db_id))
        exists = c.execute('SELECT 1 FROM schemas WHERE database=?', (key,)).fetchone()
        if exists:
            c.execute('DELETE FROM schemas WHERE database=?', (db_id,))
        else:
            c.execute('UPDATE schemas SET database=? WHERE database=?', (key, db_id))
        print('[db] 表标识迁移：%s → %s（%d 条记录）' % (title, key, n))
    # 没有 schema 行、但挂着老 ID 的孤儿记录：按"表名为空"兜底不动，仅告警
    orphan = [r[0] for r in c.execute(
        'SELECT DISTINCT database FROM records').fetchall() if r[0] not in TABLES.values()]
    if orphan:
        print('[db] 警告：存在未知表标识的记录：%s' % ', '.join(orphan))


def now():
    return time.strftime('%Y-%m-%dT%H:%M:%S')


# ── schema ────────────────────────────────────────────────────────────
def put_schema(database, title, properties):
    c = connect()
    with _lock:
        c.execute('INSERT INTO schemas(database,title,properties) VALUES(?,?,?) '
                  'ON CONFLICT(database) DO UPDATE SET title=excluded.title, properties=excluded.properties',
                  (database, title, json.dumps(properties, ensure_ascii=False)))
        c.commit()


def get_schema(database):
    c = connect()
    with _lock:
        row = c.execute('SELECT properties, title FROM schemas WHERE database=?', (database,)).fetchone()
    if not row:
        return {'properties': [], 'title': database}
    return {'properties': json.loads(row['properties']), 'title': row['title'] or database}


def all_schemas():
    c = connect()
    with _lock:
        rows = c.execute('SELECT database, title, properties FROM schemas').fetchall()
    return {r['database']: {'title': r['title'], 'properties': json.loads(r['properties'])} for r in rows}


# ── records ───────────────────────────────────────────────────────────
def _next_seq(c, database):
    row = c.execute('SELECT MAX(seq) AS m FROM records WHERE database=?', (database,)).fetchone()
    return (row['m'] or 0) + 1


def _flat(props):
    """把 SDK 风格的 {text:...}/{select:...}/{date:...} 拍平成展示值，页面直接读字段"""
    out = {}
    for k, v in (props or {}).items():
        if isinstance(v, dict):
            if 'select' in v:
                out[k] = v['select']
            elif 'text' in v:
                out[k] = v['text']
            elif 'date' in v:
                out[k] = v['date']
            elif 'link' in v:
                out[k] = v['link']
            elif 'url' in v:
                out[k] = v['url']
            elif 'multi_select' in v:
                out[k] = v['multi_select']
            else:
                out[k] = v
        else:
            out[k] = v
    return out


def add_record(database, props, source=None, person_id=None):
    c = connect()
    rid = 'rec_' + uuid.uuid4().hex[:16]
    data = _flat(props)
    with _lock:
        seq = _next_seq(c, database)
        c.execute('INSERT INTO records(id,database,data,seq,source,person_id,updated_at) VALUES(?,?,?,?,?,?,?)',
                  (rid, database, json.dumps(data, ensure_ascii=False), seq, source, person_id, now()))
        c.commit()
    return rid


def update_record(database, rid, props, person_id=None):
    c = connect()
    with _lock:
        row = c.execute('SELECT data FROM records WHERE id=? AND database=?', (rid, database)).fetchone()
        if not row:
            return False
        data = json.loads(row['data'])
        data.update(_flat(props))
        if person_id:
            c.execute('UPDATE records SET data=?, person_id=?, updated_at=? WHERE id=?',
                      (json.dumps(data, ensure_ascii=False), person_id, now(), rid))
        else:
            c.execute('UPDATE records SET data=?, updated_at=? WHERE id=?',
                      (json.dumps(data, ensure_ascii=False), now(), rid))
        c.commit()
    return True


def delete_record(database, rid):
    c = connect()
    with _lock:
        # 注意参数顺序：id 在前、database 在后（早前写反了，导致删除静默失效）
        c.execute('DELETE FROM records WHERE id=? AND database=?', (rid, database))
        c.commit()
    return True


def record_person(database, rid):
    """返回记录归属 person_id；记录不存在返回 None（用于 PUT/DELETE 的权限校验）"""
    c = connect()
    with _lock:
        row = c.execute('SELECT person_id FROM records WHERE id=? AND database=?',
                        (rid, database)).fetchone()
    if not row:
        return None
    return row['person_id'] or ''


def assign_person(old_person_id, new_person_id):
    """人员合并时，把记录归属改到新 person_id"""
    c = connect()
    with _lock:
        c.execute('UPDATE records SET person_id=? WHERE person_id=?', (new_person_id, old_person_id))
        c.commit()


def assign_unowned(person_id):
    """把历史无归属的记录划给指定人员（老库迁移用）"""
    c = connect()
    marks = ','.join('?' * len(PUBLIC_TABLES))
    with _lock:
        c.execute('UPDATE records SET person_id=? WHERE person_id IS NULL AND database NOT IN (%s)'
                  % marks, [person_id] + list(PUBLIC_TABLES))
        c.commit()


def _cmp(field, direction):
    def f(a, b):
        x, y = a.get(field), b.get(field)
        if x == y:
            return 0
        if x is None or x == '':
            return 1
        if y is None or y == '':
            return -1
        r = -1 if str(x) < str(y) else 1
        return -r if direction == 'descending' else r
    return f


def match_filter(rec, f):
    """SmartPage 过滤 DSL 的最小子集：and / or / property(equals|not|contains|notContains)"""
    if not f:
        return True
    if isinstance(f, list):
        return all(match_filter(rec, x) for x in f)
    if 'and' in f:
        return all(match_filter(rec, x) for x in (f['and'] or []))
    if 'or' in f:
        return any(match_filter(rec, x) for x in (f['or'] or []))
    p = f.get('property')
    if not isinstance(p, dict):
        return True
    name = p.get('property')
    cur = rec.get(name)
    cur_s = '' if cur is None else str(cur)
    for kind in ('text', 'number', 'date', 'select', 'checkbox', 'multiSelect'):
        ops = p.get(kind)
        if not isinstance(ops, dict):
            continue
        for op, arg in ops.items():
            a = '' if arg is None else str(arg)
            if op == 'equals':
                ok = cur_s.strip() == a.strip()
            elif op in ('not', 'isNot'):
                ok = cur_s.strip() != a.strip()
            elif op == 'contains':
                ok = a.lower() in cur_s.lower()
            elif op == 'notContains':
                ok = a.lower() not in cur_s.lower()
            elif op == 'isEmpty':
                ok = cur_s.strip() == ''
            elif op == 'isNotEmpty':
                ok = cur_s.strip() != ''
            elif op == 'gt':
                ok = cur_s.strip() != '' and cur_s > a
            elif op == 'lt':
                ok = cur_s.strip() != '' and cur_s < a
            elif op in ('gte', 'lte'):
                ok = cur_s.strip() != '' and (cur_s >= a if op == 'gte' else cur_s <= a)
            else:
                ok = True
            if not ok:
                return False
    return True


def _sort_key(v):
    """统一排序键：数字 < 字符串 < 空。
    资料表里「排序」有的行是数字、有的行缺省，直接比较会 str<int 崩溃。"""
    if v is None or v == '':
        return (2, 0.0, '')
    if isinstance(v, (int, float)):
        return (0, float(v), '')
    sv = str(v).strip()
    try:
        return (0, float(sv), '')
    except ValueError:
        return (1, 0.0, sv)


def query(database, sort=None, direction='ascending', size=200, cursor=None, person_id=None,
          filters=None):
    """person_id 不为 None 时只返回该人的记录（公共表不过滤）"""
    c = connect()
    with _lock:
        if person_id and database not in PUBLIC_TABLES:
            rows = c.execute(
                'SELECT id, data, seq FROM records WHERE database=? AND person_id=? '
                'ORDER BY seq DESC', (database, person_id)).fetchall()
        else:
            rows = c.execute('SELECT id, data, seq FROM records WHERE database=? ORDER BY seq DESC',
                             (database,)).fetchall()
    recs = []
    for r in rows:
        d = json.loads(r['data'])
        d['_id'] = r['id']
        d['record_id'] = r['id']
        if filters and not match_filter(d, filters):
            continue
        recs.append(d)
    if sort:
        recs.sort(key=lambda r: _sort_key(r.get(sort)), reverse=(direction == 'descending'))

    start = int(cursor or 0) if str(cursor or '0').isdigit() else 0
    page = recs[start:start + size]
    more = start + size < len(recs)
    return {'results': page, 'hasMore': more, 'nextCursor': str(start + size) if more else None}


def aggregate(database, aggregates, person_id=None, filters=None):
    """服务端聚合：aggregates=[{name,type:count|sum|avg|max|min,groupBy,property}]
    返回 {'results':[{name, value}]}；有 groupBy 时 value 为 [{key,count}] 或 [{key,value}]"""
    res = query(database, size=1000000, person_id=person_id, filters=filters)
    recs = res.get('results') or []

    def num(v):
        try:
            return float(str(v).replace(',', '').replace('¥', '').strip())
        except Exception:
            return None

    out = []
    for a in aggregates or []:
        name = a.get('name') or ''
        typ = (a.get('type') or 'count').lower()
        prop = a.get('property')
        gb = a.get('groupBy')
        if not gb:
            if typ == 'count':
                out.append({'name': name, 'value': len(recs)})
            else:
                vals = [num(r.get(prop)) for r in recs]
                vals = [v for v in vals if v is not None]
                if not vals:
                    out.append({'name': name, 'value': 0})
                elif typ == 'sum':
                    out.append({'name': name, 'value': round(sum(vals), 2)})
                elif typ == 'avg':
                    out.append({'name': name, 'value': round(sum(vals) / len(vals), 2)})
                elif typ == 'max':
                    out.append({'name': name, 'value': max(vals)})
                elif typ == 'min':
                    out.append({'name': name, 'value': min(vals)})
                else:
                    out.append({'name': name, 'value': 0})
            continue
        buckets = {}
        for r in recs:
            k = r.get(gb)
            k = '' if k is None else str(k)
            b = buckets.setdefault(k, {'key': k, 'count': 0, '_sum': 0.0})
            b['count'] += 1
            if typ != 'count':
                v = num(r.get(prop))
                if v is not None:
                    b['_sum'] += v
        items = []
        for b in buckets.values():
            if typ == 'count':
                items.append({'key': b['key'], 'count': b['count']})
            else:
                items.append({'key': b['key'], 'count': b['count'], 'value': round(b['_sum'], 2)})
        items.sort(key=lambda x: -x['count'])
        out.append({'name': name, 'value': items})
    return {'results': out}


def find_record(database, field, value, person_id=None):
    """按某字段找记录 id（用于去重），返回 rid 或 None"""
    c = connect()
    with _lock:
        if person_id:
            rows = c.execute('SELECT id, data FROM records WHERE database=? AND person_id=?',
                             (database, person_id)).fetchall()
        else:
            rows = c.execute('SELECT id, data FROM records WHERE database=?', (database,)).fetchall()
    for r in rows:
        d = json.loads(r['data'])
        if str(d.get(field) or '').strip() == str(value or '').strip():
            return r['id']
    return None


def find_record_rec(database, field, value, person_id=None):
    """按某字段找记录，返回 (rid, data)（找不到返回 (None, None)）"""
    c = connect()
    with _lock:
        if person_id:
            rows = c.execute('SELECT id, data FROM records WHERE database=? AND person_id=?',
                             (database, person_id)).fetchall()
        else:
            rows = c.execute('SELECT id, data FROM records WHERE database=?', (database,)).fetchall()
    for r in rows:
        d = json.loads(r['data'])
        if str(d.get(field) or '').strip() == str(value or '').strip():
            return r['id'], d
    return None, None


def count(database, person_id=None):
    c = connect()
    with _lock:
        if person_id and database not in PUBLIC_TABLES:
            return c.execute('SELECT COUNT(*) AS n FROM records WHERE database=? AND person_id=?',
                             (database, person_id)).fetchone()['n']
        return c.execute('SELECT COUNT(*) AS n FROM records WHERE database=?', (database,)).fetchone()['n']


# ── config ────────────────────────────────────────────────────────────
DEFAULTS = {
    'app_key': '', 'app_secret': '', 'corp_id': '', 'user_id': '',
    'interval_min': '30', 'auto_sync': '1', 'app_name': '', 'owner_name': '',
    'admin:pass_hash': '', 'last_sync': '',
}


def cfg_all():
    c = connect()
    with _lock:
        rows = c.execute('SELECT k, v FROM config').fetchall()
    d = dict(DEFAULTS)
    d.update({r['k']: r['v'] for r in rows})
    return d


def cfg_get(k, default=''):
    return cfg_all().get(k, default)


def cfg_set(many):
    c = connect()
    with _lock:
        for k, v in many.items():
            c.execute('INSERT INTO config(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v', (k, str(v)))
        c.commit()


def cfg_has_any(keys):
    cfg = cfg_all()
    return all((cfg.get(k) or '').strip() for k in keys)


# ── persons / accounts ────────────────────────────────────────────────
def person_new(name='', status='bound', match_by='', phones=None, emails=None, source=''):
    c = connect()
    pid = 'p_' + uuid.uuid4().hex[:12]
    with _lock:
        c.execute('INSERT INTO persons(person_id,name,status,match_by,phones,emails,source,created_at,updated_at) '
                  'VALUES(?,?,?,?,?,?,?,?,?)',
                  (pid, name, status, match_by, json.dumps(phones or [], ensure_ascii=False),
                   json.dumps(emails or [], ensure_ascii=False), source, now(), now()))
        c.commit()
    return pid


def person_set(pid, **kw):
    if not kw:
        return False
    c = connect()
    cols, vals = [], []
    for k, v in kw.items():
        if k in ('phones', 'emails') and not isinstance(v, str):
            v = json.dumps(v, ensure_ascii=False)
        cols.append('%s=?' % k)
        vals.append(v)
    cols.append('updated_at=?')
    vals.append(now())
    vals.append(pid)
    with _lock:
        c.execute('UPDATE persons SET %s WHERE person_id=?' % ','.join(cols), vals)
        c.commit()
    return True


def persons_all(status=None):
    c = connect()
    with _lock:
        if status:
            rows = c.execute('SELECT * FROM persons WHERE status=? ORDER BY name', (status,)).fetchall()
        else:
            rows = c.execute('SELECT * FROM persons ORDER BY name').fetchall()
    out = []
    for r in rows:
        d = dict(r)
        for k in ('phones', 'emails'):
            try:
                d[k] = json.loads(d.get(k) or '[]')
            except Exception:
                d[k] = []
        out.append(d)
    return out


def person_get(pid):
    for p in persons_all():
        if p['person_id'] == pid:
            return p
    return None


def person_del(pid):
    c = connect()
    with _lock:
        c.execute('DELETE FROM persons WHERE person_id=?', (pid,))
        c.execute('DELETE FROM accounts WHERE person_id=?', (pid,))
        c.commit()
    return True


def account_add(pid, platform, account_id, name='', extra=None):
    c = connect()
    extra_s = json.dumps(extra or {}, ensure_ascii=False) if not isinstance(extra, str) else extra
    with _lock:
        c.execute('INSERT INTO accounts(person_id,platform,account_id,name,extra) VALUES(?,?,?,?,?) '
                  'ON CONFLICT(person_id,platform,account_id) DO UPDATE SET name=excluded.name, extra=excluded.extra',
                  (pid, platform, account_id, name, extra_s))
        c.commit()
    return True


def account_del(pid, platform, account_id):
    c = connect()
    with _lock:
        c.execute('DELETE FROM accounts WHERE person_id=? AND platform=? AND account_id=?',
                  (pid, platform, account_id))
        c.commit()
    return True


def accounts_of(pid, platform=None):
    c = connect()
    with _lock:
        if platform:
            rows = c.execute('SELECT * FROM accounts WHERE person_id=? AND platform=?',
                             (pid, platform)).fetchall()
        else:
            rows = c.execute('SELECT * FROM accounts WHERE person_id=?', (pid,)).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        try:
            d['extra'] = json.loads(d.get('extra') or '{}')
        except Exception:
            d['extra'] = {}
        out.append(d)
    return out


def account_find(platform, account_id):
    c = connect()
    with _lock:
        row = c.execute('SELECT * FROM accounts WHERE platform=? AND account_id=?',
                        (platform, account_id)).fetchone()
    if not row:
        return None
    d = dict(row)
    try:
        d['extra'] = json.loads(d.get('extra') or '{}')
    except Exception:
        d['extra'] = {}
    return d


def accounts_all(platform=None):
    c = connect()
    with _lock:
        if platform:
            rows = c.execute('SELECT * FROM accounts WHERE platform=?', (platform,)).fetchall()
        else:
            rows = c.execute('SELECT * FROM accounts').fetchall()
    return [dict(r) for r in rows]


def person_find_by_contact(phone='', email=''):
    """按手机/邮箱任一命中找人（匹配阶段的辅助函数）"""
    phones = set(normalize_phones([phone]))
    emails = set(normalize_emails([email]))
    for p in persons_all():
        if phones & set(p.get('phones') or []):
            return p
        if emails & set(p.get('emails') or []):
            return p
    return None


def normalize_phones(vals):
    out = []
    for v in vals or []:
        s = ''.join(ch for ch in str(v or '') if ch.isdigit())
        if s.startswith('0086'):
            s = s[4:]
        elif s.startswith('86') and len(s) == 13:
            s = s[2:]
        if len(s) >= 7:
            out.append(s)
    return out


def normalize_emails(vals):
    out = []
    for v in vals or []:
        s = str(v or '').strip().lower()
        if '@' in s and len(s) >= 5:
            out.append(s)
    return out


# ── sessions ──────────────────────────────────────────────────────────
def session_create(kind, person_id='', name='', days=7):
    import secrets
    c = connect()
    tok = secrets.token_urlsafe(32)
    exp = time.strftime('%Y-%m-%dT%H:%M:%S', time.localtime(time.time() + days * 86400))
    with _lock:
        # 顺手清掉已过期的会话，避免 sessions 表只增不减
        try:
            c.execute('DELETE FROM sessions WHERE expires < ?', (now(),))
        except Exception:
            pass
        c.execute('INSERT INTO sessions(token,kind,person_id,name,created_at,expires) VALUES(?,?,?,?,?,?)',
                  (tok, kind, person_id, name, now(), exp))
        c.commit()
    return tok, exp


def session_get(token):
    if not token:
        return None
    c = connect()
    with _lock:
        row = c.execute('SELECT * FROM sessions WHERE token=?', (token,)).fetchone()
    if not row:
        return None
    d = dict(row)
    if str(d.get('expires') or '') < now():
        session_delete(token)
        return None
    return d


def session_delete(token):
    c = connect()
    with _lock:
        c.execute('DELETE FROM sessions WHERE token=?', (token,))
        c.commit()


def sessions_clear(kind=None):
    c = connect()
    with _lock:
        if kind:
            c.execute('DELETE FROM sessions WHERE kind=?', (kind,))
        else:
            c.execute('DELETE FROM sessions')
        c.commit()


# ── sync log ──────────────────────────────────────────────────────────
def log(kind, ok, rows=0, msg=''):
    c = connect()
    with _lock:
        c.execute('INSERT INTO sync_log(at,kind,ok,rows,msg) VALUES(?,?,?,?,?)',
                  (now(), kind, 1 if ok else 0, rows, msg[:800]))
        c.execute('DELETE FROM sync_log WHERE id NOT IN (SELECT id FROM sync_log ORDER BY id DESC LIMIT 300)')
        c.commit()


def logs(limit=30):
    c = connect()
    with _lock:
        rows = c.execute('SELECT at,kind,ok,rows,msg FROM sync_log ORDER BY id DESC LIMIT ?', (limit,)).fetchall()
    return [dict(r) for r in rows]


def stats(person_id=None):
    return {k: count(v, person_id) for k, v in TABLES.items()}


# ── 审批 Agent 判断记录（用于校准一致率）────────────────────────────
def judge_save(pid, person_id, title, typ, verdict, confidence, payload, model='', cost_ms=0):
    c = connect()
    jid = 'jd_' + uuid.uuid4().hex[:14]
    with _lock:
        c.execute('INSERT INTO judge_log(id,pid,person_id,title,typ,verdict,confidence,'
                  'payload,model,cost_ms,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                  (jid, pid, person_id, title, typ, verdict, confidence,
                   json.dumps(payload, ensure_ascii=False), model, int(cost_ms), now()))
        c.commit()
    return jid


def judge_get(pid, person_id=''):
    """取某单最近一次判断；person_id 非空时只认本人的"""
    c = connect()
    sql = 'SELECT * FROM judge_log WHERE pid=?'
    args = [pid]
    if person_id:
        sql += ' AND person_id=?'
        args.append(person_id)
    sql += ' ORDER BY created_at DESC, rowid DESC LIMIT 1'
    with _lock:
        r = c.execute(sql, args).fetchone()
    if not r:
        return None
    d = dict(r)
    try:
        d['payload'] = json.loads(d.get('payload') or '{}')
    except Exception:
        d['payload'] = {}
    return d


def judge_feedback(pid, adopted, person_id=''):
    """记录人是否采纳（1 采纳 / 0 不采纳），用于算一致率"""
    c = connect()
    sql = 'UPDATE judge_log SET adopted=?, fb_at=? WHERE pid=?'
    args = [1 if adopted else 0, now(), pid]
    if person_id:
        sql += ' AND person_id=?'
        args.append(person_id)
    with _lock:
        cur = c.execute(sql, args)
        c.commit()
    return cur.rowcount


def judge_stats(days=30, person_id=''):
    c = connect()
    since = time.strftime('%Y-%m-%dT%H:%M:%S', time.localtime(time.time() - days * 86400))
    sql = 'SELECT verdict, adopted, COUNT(*) n FROM judge_log WHERE created_at>=?'
    args = [since]
    if person_id:
        sql += ' AND person_id=?'
        args.append(person_id)
    sql += ' GROUP BY verdict, adopted'
    with _lock:
        rows = c.execute(sql, args).fetchall()
    total = sum(r['n'] for r in rows)
    fb = sum(r['n'] for r in rows if r['adopted'] is not None)
    ok = sum(r['n'] for r in rows if r['adopted'] == 1)
    by = {}
    for r in rows:
        by[r['verdict']] = by.get(r['verdict'], 0) + r['n']
    return {'total': total, 'feedback': fb, 'adopted': ok,
            'agreement': (round(ok * 100.0 / fb) if fb else None),
            'by_verdict': by, 'days': days}
