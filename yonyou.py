# -*- coding: utf-8 -*-
"""用友开放平台适配器（YonBIP / YonSuite，纯标准库）

认证（自建应用模式）：
  POST {host}/iuap-api-auth/open-auth/selfAppAuth/getAccessToken
  {"appKey": ..., "appSecret": ...} → data.access_token
拉取：待办/单据接口路径可在平台配置里自定义（todo_path），
      返回结构兼容 data 列表 / data.recordList / data.content
说明：用友不同版本（YonBIP/YonSuite/U8 cloud）待办端点差异较大，
      默认端点开放，管理员按开放平台文档填写待办接口路径即可联动。
"""
import json
import urllib.error
import urllib.request

TIMEOUT = 20

DEFAULT_HOST = 'https://api.yonyou.com'
DEFAULT_AUTH_PATH = '/iuap-api-auth/open-auth/selfAppAuth/getAccessToken'


class YonyouError(Exception):
    def __init__(self, msg, raw=None):
        super().__init__(msg)
        self.raw = raw


class Client:
    def __init__(self, host='', app_key='', app_secret='', ent_code='', user='', todo_path=''):
        h = (host or DEFAULT_HOST).strip().rstrip('/')
        if not h.startswith('http'):
            h = 'https://' + h
        self.host = h
        self.app_key = (app_key or '').strip()
        self.app_secret = (app_secret or '').strip()
        self.ent_code = (ent_code or '').strip()
        self.user = (user or '').strip()
        self.todo_path = (todo_path or '').strip()
        self.token = ''
        self._at = 0

    def token(self):
        if not self.app_key or not self.app_secret:
            raise YonyouError('请填写用友 AppKey / AppSecret')
        if self.token and __import__('time').time() - self._at < 3000:
            return self.token
        body = {'appKey': self.app_key, 'appSecret': self.app_secret}
        if self.ent_code:
            body['entCode'] = self.ent_code
        req = urllib.request.Request(self.host + DEFAULT_AUTH_PATH,
                                     data=json.dumps(body).encode('utf-8'), method='POST')
        req.add_header('Content-Type', 'application/json')
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
                txt = r.read().decode('utf-8', 'replace')
        except urllib.error.HTTPError as e:
            raise YonyouError('认证 HTTP %s（检查 AppKey/Secret、服务器出口IP 白名单）' % e.code,
                              raw=e.read().decode('utf-8', 'replace')[:400])
        except Exception as e:
            raise YonyouError('连接用友开放平台失败：%s' % e)
        try:
            d = json.loads(txt)
        except Exception:
            raise YonyouError('认证返回非 JSON', raw=txt[:300])
        tok = ((d.get('data') or {}).get('access_token') or d.get('access_token') or '').strip()
        if not tok:
            raise YonyouError('未取到 access_token：%s' % (d.get('message') or d.get('msg') or txt[:150]))
        import time as _t
        self.token, self._at = tok, _t.time()
        return tok

    def _get(self, path, params=''):
        self.token()
        url = self.host + path + (('?' + params) if params else '')
        req = urllib.request.Request(url, method='GET')
        req.add_header('access_token', self.token)
        req.add_header('Accept', 'application/json')
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
                txt = r.read().decode('utf-8', 'replace')
        except urllib.error.HTTPError as e:
            raise YonyouError('HTTP %s %s' % (e.code, path[:60]),
                              raw=e.read().decode('utf-8', 'replace')[:400])
        except Exception as e:
            raise YonyouError('请求失败：%s' % e)
        try:
            return json.loads(txt)
        except Exception:
            raise YonyouError('返回非 JSON', raw=txt[:300])

    def todo_items(self):
        """拉待办列表（路径来自平台配置 todo_path）"""
        if not self.todo_path:
            raise YonyouError('请在平台配置里填写待办接口路径（如 /yonbip/.../todo/list）')
        params = ('user=%s' % urllib.parse.quote(self.user)) if self.user else ''
        d = self._get(self.todo_path, params)
        data = d.get('data') if isinstance(d, dict) else d
        if isinstance(data, dict):
            rows = data.get('recordList') or data.get('records') or data.get('content') or data.get('list') or []
        else:
            rows = data or []
        out = []
        for it in rows:
            if not isinstance(it, dict):
                continue
            iid = str(it.get('id') or it.get('billnumber') or it.get('code') or it.get('pk') or '')
            title = str(it.get('name') or it.get('title') or it.get('subject') or it.get('summary') or '').strip()
            if not iid or not title:
                continue
            st = str(it.get('status') or it.get('billStatus') or '')
            out.append({
                'id': iid,
                'title': title,
                'status': STATUS_MAP.get(st, st or '待处理'),
                'link': str(it.get('url') or it.get('link') or ''),
                'date': str(it.get('createTime') or it.get('date') or '')[:10],
                'creator': str(it.get('creator') or it.get('creatorName') or ''),
            })
        return out

    def users(self):
        raise YonyouError('用友暂无标准通讯录接口，请在人员绑定里手工填写用户编码')


STATUS_MAP = {
    '1': '待处理', '2': '进行中', '3': '已完成', '4': '已撤销',
    'PENDING': '待处理', 'DOING': '进行中', 'DONE': '已完成', 'CANCEL': '已撤销',
    'A': '待处理', 'B': '进行中', 'C': '已完成',
}
