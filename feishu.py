# -*- coding: utf-8 -*-
"""飞书适配器（纯标准库）

接口
1. tenant_access_token   POST /open-apis/auth/v3/tenant_access_token/internal
2. 通讯录成员            GET  /open-apis/contact/v3/users/find_by_department
3. 待办审批任务          POST /open-apis/approval/v4/tasks/query
4. 我发起的审批          POST /open-apis/approval/v4/instances/query
"""
import json
import time
import urllib.error
import urllib.parse
import urllib.request

BASE = 'https://open.feishu.cn'
TIMEOUT = 15


class FeishuError(Exception):
    def __init__(self, msg, code=None, raw=None):
        super().__init__(msg)
        self.code = code
        self.raw = raw


def _req(url, data=None, headers=None, method=None):
    body = json.dumps(data, ensure_ascii=False).encode('utf-8') if data is not None else None
    req = urllib.request.Request(url, data=body, method=method or ('POST' if body else 'GET'))
    req.add_header('Content-Type', 'application/json; charset=utf-8')
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            txt = r.read().decode('utf-8', 'replace')
    except urllib.error.HTTPError as e:
        raise FeishuError('HTTP %s' % e.code, code=e.code, raw=e.read().decode('utf-8', 'replace')[:400])
    except Exception as e:
        raise FeishuError('网络请求失败：%s' % e)
    try:
        return json.loads(txt)
    except Exception:
        raise FeishuError('返回内容不是 JSON', raw=txt[:300])


STATUS_MAP = {
    'PENDING': '进行中', 'APPROVED': '已完成', 'REJECTED': '已拒绝',
    'CANCELED': '已撤销', 'DELETED': '已撤销', 'TERMINATED': '已撤销',
}


class Client:
    def __init__(self, app_id, app_secret):
        self.app_id = (app_id or '').strip()
        self.app_secret = (app_secret or '').strip()
        self._token = None
        self._at = 0

    def token(self):
        if self._token and time.time() - self._at < 6600:
            return self._token
        if not self.app_id or not self.app_secret:
            raise FeishuError('请先填写飞书 App ID / App Secret')
        d = _req(BASE + '/open-apis/auth/v3/tenant_access_token/internal',
                 {'app_id': self.app_id, 'app_secret': self.app_secret})
        if d.get('code'):
            raise FeishuError('获取 tenant_access_token 失败：%s' % d.get('msg'), code=d.get('code'))
        self._token = d.get('tenant_access_token')
        self._at = time.time()
        return self._token

    def _api(self, path, query=None, data=None, method=None):
        url = BASE + path
        if query:
            url += '?' + urllib.parse.urlencode(query)
        d = _req(url, data, headers={'Authorization': 'Bearer ' + self.token()}, method=method)
        if d.get('code'):
            raise FeishuError('%s 失败：%s' % (path, d.get('msg')), code=d.get('code'))
        return d.get('data') or d

    # ── 通讯录 ────────────────────────────────────────────
    def users(self, max_pages=40):
        """根部门下全部成员（首次接入用）"""
        out, page_token, pages = [], '', 0
        while pages < max_pages:
            q = {'department_id': '0', 'department_id_type': 'open_department_id',
                 'page_size': 50, 'user_id_type': 'open_id'}
            if page_token:
                q['page_token'] = page_token
            d = self._api('/open-apis/contact/v3/users/find_by_department', q)
            for u in d.get('items') or []:
                out.append({
                    'open_id': u.get('open_id') or '',
                    'union_id': u.get('union_id') or '',
                    'name': u.get('name') or '',
                    'mobile': u.get('mobile') or '',
                    'email': u.get('email') or u.get('enterprise_email') or '',
                    'job_number': u.get('job_number') or '',
                })
            pages += 1
            if not d.get('has_more'):
                break
            page_token = d.get('page_token') or ''
        return out

    # ── 审批 ──────────────────────────────────────────────
    def approval_tasks(self, open_id, task_status='PENDING', max_pages=5):
        out, page_token, pages = [], '', 0
        while pages < max_pages:
            body = {'user_id': open_id, 'page_size': 50, 'task_status': task_status}
            if page_token:
                body['page_token'] = page_token
            d = self._api('/open-apis/approval/v4/tasks/query', {'user_id_type': 'open_id'}, body)
            for t in d.get('tasks') or []:
                out.append({
                    'task_id': str(t.get('id') or t.get('task_id') or ''),
                    'instance_code': t.get('instance_code') or t.get('process_code') or '',
                    'title': t.get('title') or t.get('instance_code') or '飞书审批',
                    'status': STATUS_MAP.get(str(t.get('status') or ''), '进行中'),
                    'create_time': t.get('create_time') or t.get('update_time') or '',
                    'url': t.get('url') or '',
                })
            pages += 1
            if not d.get('has_more'):
                break
            page_token = d.get('page_token') or ''
        return out

    def approval_instances(self, open_id, max_pages=3):
        out, page_token, pages = [], '', 0
        while pages < max_pages:
            body = {'user_id': open_id, 'page_size': 50}
            if page_token:
                body['page_token'] = page_token
            d = self._api('/open-apis/approval/v4/instances/query', {'user_id_type': 'open_id'}, body)
            for t in d.get('instance_list') or d.get('items') or []:
                out.append({
                    'instance_code': t.get('instance_code') or '',
                    'title': t.get('approval_name') or t.get('title') or '飞书审批',
                    'status': STATUS_MAP.get(str(t.get('status') or ''), '进行中'),
                    'create_time': t.get('start_time') or '',
                    'url': t.get('url') or '',
                })
            pages += 1
            if not d.get('has_more'):
                break
            page_token = d.get('page_token') or ''
        return out
