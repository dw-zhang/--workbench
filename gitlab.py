# -*- coding: utf-8 -*-
"""GitLab 适配器（自建 / gitlab.com 通用，纯标准库）

认证：Personal Access Token（scope: read_api + read_user）
拉取：issues（指派给我）+ merge_requests（指派/评审给我）
人员：GET /users?active=true
"""
import json
import time
import urllib.error
import urllib.parse
import urllib.request

TIMEOUT = 20


class GitlabError(Exception):
    def __init__(self, msg, raw=None):
        super().__init__(msg)
        self.raw = raw


class Client:
    def __init__(self, host, token):
        h = (host or '').strip().rstrip('/')
        if h and not h.startswith('http'):
            h = 'https://' + h
        self.host = h
        self.api = h + '/api/v4'
        self.token = (token or '').strip()

    def _req(self, url):
        req = urllib.request.Request(url, method='GET')
        req.add_header('PRIVATE-TOKEN', self.token)
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
                txt = r.read().decode('utf-8', 'replace')
        except urllib.error.HTTPError as e:
            raise GitlabError('HTTP %s %s' % (e.code, url.split('/api/v4')[-1][:60]))
        except Exception as e:
            raise GitlabError('连接 GitLab 失败：%s' % e)
        try:
            return json.loads(txt)
        except Exception:
            raise GitlabError('返回非 JSON', raw=txt[:300])

    def _paged(self, path, params='', pages=8):
        out, page = [], 1
        while page <= pages:
            sep = '&' if params else ''
            rows = self._req('%s%s?%s%sper_page=100&page=%d' % (self.api, path, params, sep, page))
            if not rows:
                break
            out.extend(rows)
            if not isinstance(rows[0], dict) or len(rows) < 100:
                break
            page += 1
        return out

    def me(self):
        d = self._req(self.api + '/user')
        return {'username': d.get('username', ''), 'name': d.get('name', '')}

    def users(self):
        rows = self._paged('/users', 'active=true&order_by=name')
        return [{'account': str(u.get('username') or '').strip(),
                 'name': str(u.get('name') or '').strip(),
                 'email': str(u.get('public_email') or '').strip(),
                 'mobile': ''} for u in rows]

    def issues(self, username, days=90):
        since = time.strftime('%Y-%m-%dT00:00:00Z', time.gmtime(time.time() - days * 86400))
        rows = self._paged('/issues', 'scope=all&assignee_username=%s&updated_after=%s&order_by=updated_at'
                           % (urllib.parse.quote(username), since))
        out = []
        for it in rows:
            state = str(it.get('state') or '')
            out.append({
                'id': str(it.get('id') or ''),
                'title': str(it.get('title') or '').strip(),
                'status': '已完成' if state == 'closed' else '待处理',
                'link': str(it.get('web_url') or ''),
                'repo': ((it.get('project_id') and (it.get('references') or {}).get('full', '').split('#')[0]) or ''),
                'labels': ','.join(it.get('labels') or []),
                'updated': str(it.get('updated_at') or '')[:10],
                'kind': 'issue',
            })
        return out

    def merge_requests(self, username, days=90):
        since = time.strftime('%Y-%m-%dT00:00:00Z', time.gmtime(time.time() - days * 86400))
        rows = self._paged('/merge_requests',
                           'scope=all&assignee_username=%s&updated_after=%s&order_by=updated_at'
                           % (urllib.parse.quote(username), since))
        out = []
        for it in rows:
            state = str(it.get('state') or '')
            out.append({
                'id': 'mr-%s' % (it.get('iid') or ''),
                'title': str(it.get('title') or '').strip(),
                'status': '已完成' if state in ('merged', 'closed') else '进行中',
                'link': str(it.get('web_url') or ''),
                'repo': (it.get('references') or {}).get('full', '').split('!')[0],
                'labels': ','.join(it.get('labels') or []),
                'updated': str(it.get('updated_at') or '')[:10],
                'kind': 'mr',
            })
        return out
