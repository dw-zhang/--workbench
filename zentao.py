# -*- coding: utf-8 -*-
"""禅道 ZenTao 适配器（纯标准库）

流程
1. POST {host}/api.php/v1/tokens  {"account","password"} → token（禅道 ≥ 15 REST v1）
2. GET /tasks /bugs /todos  → 按负责人过滤
3. GET /users → 人员绑定

状态映射
  任务：wait 待处理 / doing 进行中 / pause 进行中 / done 已完成 / cancel 已撤销
  Bug ：active 待处理 / resolved 进行中 / closed 已完成
  待办：wait 待处理 / doing 进行中 / done 已完成
"""
import json
import urllib.error
import urllib.request

TIMEOUT = 20

TASK_STATUS = {'wait': '待处理', 'doing': '进行中', 'pause': '进行中',
               'done': '已完成', 'closed': '已完成', 'cancel': '已撤销'}
BUG_STATUS = {'active': '待处理', 'resolved': '进行中', 'verified': '进行中',
              'closed': '已完成', 'willnotfix': '已撤销'}
TODO_STATUS = {'wait': '待处理', 'doing': '进行中', 'done': '已完成',
               'closed': '已完成', 'cancel': '已撤销'}
STATUS_MAPS = {'task': TASK_STATUS, 'bug': BUG_STATUS, 'todo': TODO_STATUS}


class ZentaoError(Exception):
    def __init__(self, msg, raw=None):
        super().__init__(msg)
        self.raw = raw


class Client:
    def __init__(self, host, account, password):
        h = (host or '').strip().rstrip('/')
        if h and not h.startswith('http'):
            h = 'http://' + h
        self.host = h
        self.account = (account or '').strip()
        self.password = (password or '').strip()
        self.token = ''

    def login(self):
        if not self.host:
            raise ZentaoError('请先填写禅道地址')
        if not self.account or not self.password:
            raise ZentaoError('请填写禅道账号与密码')
        url = self.host + '/api.php/v1/tokens'
        body = json.dumps({'account': self.account, 'password': self.password}).encode('utf-8')
        req = urllib.request.Request(url, data=body, method='POST')
        req.add_header('Content-Type', 'application/json')
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
                txt = r.read().decode('utf-8', 'replace')
        except urllib.error.HTTPError as e:
            raise ZentaoError('登录 HTTP %s（检查账号密码 / 是否启用 API）' % e.code)
        except Exception as e:
            raise ZentaoError('连接禅道失败：%s' % e)
        try:
            d = json.loads(txt)
        except Exception:
            raise ZentaoError('登录返回非 JSON（确认禅道版本 ≥ 15 且开启 API）', raw=txt[:300])
        tok = (d.get('token') or (d.get('data') or {}).get('token') or '').strip()
        if not tok:
            raise ZentaoError('未取到 token：%s' % txt[:150])
        self.token = tok
        return True

    def _get(self, path, params=''):
        if not self.token:
            self.login()
        url = self.host + '/api.php/v1' + path + (('?' + params) if params else '')
        req = urllib.request.Request(url, method='GET')
        req.add_header('Token', self.token)
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
                txt = r.read().decode('utf-8', 'replace')
        except urllib.error.HTTPError as e:
            raise ZentaoError('HTTP %s %s' % (e.code, path), raw=e.read().decode('utf-8', 'replace')[:300])
        except Exception as e:
            raise ZentaoError('请求失败：%s' % e)
        try:
            return json.loads(txt)
        except Exception:
            raise ZentaoError('返回非 JSON', raw=txt[:300])

    def _list(self, path, pages=20):
        """分页拉取，返回 [{...}]"""
        out, page = [], 1
        while page <= pages:
            d = self._get(path, 'page=%d&limit=100' % page)
            rows = d.get('tasks') or d.get('bugs') or d.get('todos') or d.get('users') or d.get('stories') or []
            if not rows:
                break
            out.extend(rows)
            if len(rows) < 100:
                break
            page += 1
        return out

    @staticmethod
    def _val(v):
        """assignedTo 等字段可能是字符串或 {id,realname} 对象"""
        if isinstance(v, dict):
            return str(v.get('account') or v.get('id') or v.get('realname') or '').strip()
        return str(v or '').strip()

    def users(self):
        rows = self._list('/users')
        return [{'account': str(u.get('account') or '').strip(),
                 'name': str(u.get('realname') or u.get('account') or '').strip(),
                 'email': str(u.get('email') or '').strip(),
                 'mobile': str(u.get('mobile') or '').strip()} for u in rows]

    def items(self, kind, account, days=90):
        """某人的任务/bug/待办；kind: task|bug|todo"""
        path = {'task': '/tasks', 'bug': '/bugs', 'todo': '/todos'}[kind]
        smap = STATUS_MAPS[kind]
        out = []
        for it in self._list(path):
            who = self._val(it.get('assignedTo'))
            if who and who != account:
                continue
            iid = str(it.get('id') or '')
            title = str(it.get('name') or it.get('title') or it.get('desc') or '').strip()
            if not title:
                continue
            st = str(it.get('status') or '')
            deadline = str(it.get('deadline') or it.get('date') or '')[:10]
            opened = str(it.get('openedDate') or '')[:10]
            out.append({
                'id': '%s-%s' % (kind, iid),
                'kind': kind,
                'title': title,
                'status': smap.get(st, st or '待处理'),
                'link': '%s/%s-view-%s.html' % (self.host, kind, iid),
                'date': deadline or opened,
                'pri': str(it.get('pri') or ''),
            })
        return out
