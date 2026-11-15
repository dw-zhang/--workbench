# -*- coding: utf-8 -*-
"""Gitea / Gogs 适配器（纯标准库）

认证：Access Token（Settings → Applications → Generate Token）
拉取：按组织/仓库遍历 Issue 与 PR，按指派人过滤
人员：GET /admin/users（需管理员 Token），否则用组织成员
"""
import json
import urllib.error
import urllib.parse
import urllib.request

TIMEOUT = 20


class GiteaError(Exception):
    def __init__(self, msg, raw=None):
        super().__init__(msg)
        self.raw = raw


class Client:
    def __init__(self, host, token, org=''):
        h = (host or '').strip().rstrip('/')
        if h and not h.startswith('http'):
            h = 'https://' + h
        self.host = h
        self.api = h + '/api/v1'
        self.token = (token or '').strip()
        self.org = (org or '').strip().strip('/')

    def _req(self, url):
        req = urllib.request.Request(url, method='GET')
        req.add_header('Authorization', 'token ' + self.token)
        req.add_header('Accept', 'application/json')
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
                txt = r.read().decode('utf-8', 'replace')
        except urllib.error.HTTPError as e:
            raise GiteaError('HTTP %s %s' % (e.code, url.split('/api/v1')[-1][:60]))
        except Exception as e:
            raise GiteaError('连接 Gitea 失败：%s' % e)
        try:
            return json.loads(txt)
        except Exception:
            raise GiteaError('返回非 JSON', raw=txt[:300])

    def me(self):
        d = self._req(self.api + '/user')
        return {'username': d.get('login', ''), 'name': d.get('full_name') or d.get('login', '')}

    def users(self):
        """管理员 Token → 全部用户；否则按组织成员"""
        try:
            rows, page = [], 1
            while page <= 20:
                d = self._req('%s/admin/users?page=%d&limit=100' % (self.api, page))
                if not d:
                    break
                rows.extend(d)
                if len(d) < 100:
                    break
                page += 1
        except GiteaError:
            rows = []
            if self.org:
                rows = self._req('%s/orgs/%s/members?limit=100' % (self.api, urllib.parse.quote(self.org)))
        return [{'account': str(u.get('login') or '').strip(),
                 'name': str(u.get('full_name') or u.get('login') or '').strip(),
                 'email': str(u.get('email') or '').strip(),
                 'mobile': ''} for u in rows]

    def _repos(self, pages=20):
        if self.org:
            path = '/orgs/%s/repos' % urllib.parse.quote(self.org)
        else:
            path = '/repos/search?limit=50'
        out, page = [], 1
        while page <= pages:
            d = self._req('%s%s?page=%d&limit=50' % (self.api, path, page))
            if not d:
                break
            out.extend(d)
            if len(d) < 50:
                break
            page += 1
        return out

    def issues(self, username, days=90, kinds=('issue',)):
        """遍历仓库的 Issue/PR，按指派人过滤"""
        import time as _t
        since = _t.strftime('%Y-%m-%dT%H:%M:%SZ', _t.gmtime(_t.time() - days * 86400))
        out = []
        for repo in self._repos():
            full = str(repo.get('full_name') or '')
            if not full:
                continue
            page = 1
            while page <= 10:
                try:
                    rows = self._req('%s/repos/%s/issues?state=all&type=%s&since=%s&page=%d&limit=50'
                                     % (self.api, urllib.parse.quote(full), kinds[0], since, page))
                except GiteaError:
                    break
                if not rows:
                    break
                for it in rows:
                    assignees = [str(a.get('login') or '') for a in (it.get('assignees') or [])]
                    if username not in assignees and str(it.get('assignee') and it['assignee'].get('login') or '') != username:
                        continue
                    # Gitea 的 /issues 接口同时返回 PR（有 pull_request 字段）
                    is_pr = bool(it.get('pull_request'))
                    if is_pr and 'issue' in kinds:
                        continue
                    if not is_pr and 'pr' not in kinds:
                        continue
                    state = str(it.get('state') or '')
                    merged = bool((it.get('pull_request') or {}).get('merged'))
                    out.append({
                        'id': str(it.get('id') or ''),
                        'title': str(it.get('title') or '').strip(),
                        'status': '已完成' if (state == 'closed') else ('进行中' if is_pr else '待处理'),
                        'link': str(it.get('html_url') or ''),
                        'repo': full,
                        'labels': ','.join(str(l.get('name') or '') for l in (it.get('labels') or [])),
                        'updated': str(it.get('updated_at') or '')[:10],
                        'kind': 'pr' if is_pr else 'issue',
                        'merged': merged,
                    })
                if len(rows) < 50:
                    break
                page += 1
        return out

    def pull_requests(self, username, days=90):
        return self.issues(username, days, kinds=('pr',))
