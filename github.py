# -*- coding: utf-8 -*-
"""GitHub / Git 适配器（纯标准库）

两种取数方式
1. 拉取：GET /repos/{owner}/{repo}/issues?assignee={login}&state=all&since=...
        GET /notifications（Token 主人的通知）
2. 推送：仓库 Webhook → POST /webhook/github（HMAC 校验，本模块提供 verify + 解析）

Token 属于某一个人，所以按"每人一个账号"遍历；组织级只读 Token 也可用 assignee 过滤。
"""
import hashlib
import hmac
import json
import urllib.error
import urllib.parse
import urllib.request

BASE = 'https://api.github.com'
TIMEOUT = 15


class GithubError(Exception):
    def __init__(self, msg, code=None, raw=None):
        super().__init__(msg)
        self.code = code
        self.raw = raw


def _req(url, token, data=None, method=None, accept='application/vnd.github+json'):
    body = json.dumps(data, ensure_ascii=False).encode('utf-8') if data is not None else None
    req = urllib.request.Request(url, data=body, method=method or ('POST' if body else 'GET'))
    req.add_header('Accept', accept)
    req.add_header('X-GitHub-Api-Version', '2022-11-28')
    req.add_header('User-Agent', 'wbdash/1.0')
    if token:
        req.add_header('Authorization', 'Bearer ' + token)
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            txt = r.read().decode('utf-8', 'replace')
            link = r.headers.get('Link') or ''
    except urllib.error.HTTPError as e:
        raise GithubError('HTTP %s' % e.code, code=e.code, raw=e.read().decode('utf-8', 'replace')[:400])
    except Exception as e:
        raise GithubError('网络请求失败：%s' % e)
    try:
        return json.loads(txt), link
    except Exception:
        raise GithubError('返回内容不是 JSON', raw=txt[:300])


STATUS_MAP = {'open': '待处理', 'closed': '已完成'}


def normalize_issue(it, repo=''):
    state = str(it.get('state') or 'open')
    is_pr = bool(it.get('pull_request'))
    labels = ','.join(str(l.get('name') if isinstance(l, dict) else l) for l in (it.get('labels') or []))
    return {
        'id': '%s#%s' % (repo or (it.get('repository_url') or '').split('/repos/')[-1], it.get('number')),
        'title': ('[PR] ' if is_pr else '') + str(it.get('title') or ''),
        'status': STATUS_MAP.get(state, '待处理'),
        'url': it.get('html_url') or '',
        'updated': it.get('updated_at') or it.get('created_at') or '',
        'creator': (it.get('user') or {}).get('login') or '',
        'assignee': ','.join((a or {}).get('login', '') for a in (it.get('assignees') or [])),
        'labels': labels,
        'repo': repo,
        'kind': 'pr' if is_pr else 'issue',
    }


class Client:
    def __init__(self, token, repos=''):
        self.token = (token or '').strip()
        self.repos = [r.strip() for r in str(repos or '').split(',') if '/' in r]

    def _get(self, path):
        url = path if path.startswith('http') else BASE + path
        return _req(url, self.token)

    def issues_for(self, login, since='', per_repo=100, max_pages=3):
        out = []
        for repo in self.repos:
            path = '/repos/%s/issues?assignee=%s&state=all&per_page=%d' % (
                repo, urllib.parse.quote(login), per_repo)
            if since:
                path += '&since=' + urllib.parse.quote(since)
            page = 1
            while page <= max_pages:
                d, link = self._get(path + '&page=%d' % page)
                if not isinstance(d, list):
                    break
                out.extend(normalize_issue(it, repo) for it in d)
                if 'rel="next"' not in link:
                    break
                page += 1
        return out

    def my_issues(self, since=''):
        """Token 主人被指派的 Issue（不配 repos 时的兜底）"""
        q = '/issues?filter=assigned&state=all&per_page=50'
        if since:
            q += '&since=' + urllib.parse.quote(since)
        d, _ = self._get(q)
        return [normalize_issue(it) for it in (d or [])] if isinstance(d, list) else []

    def notifications(self, all_=False):
        d, _ = self._get('/notifications?all=%s&per_page=50' % ('true' if all_ else 'false'))
        return d if isinstance(d, list) else []


# ── Webhook ───────────────────────────────────────────────────────────
def verify_signature(secret, body_bytes, signature):
    """GitHub webhook 用 sha256 HMAC；签名头 X-Hub-Signature-256: sha256=..."""
    if not secret:
        return True
    if not signature:
        return False
    mac = hmac.new(secret.encode('utf-8'), body_bytes, hashlib.sha256).hexdigest()
    expect = 'sha256=' + mac
    return hmac.compare_digest(expect, signature.strip())


def parse_webhook(event, payload):
    """把 GitHub 事件转成统一记录结构；不认识的事件返回 None"""
    if event == 'issues' and payload.get('issue'):
        it = payload['issue']
        repo = (payload.get('repository') or {}).get('full_name', '')
        rec = normalize_issue(it, repo)
        rec['action'] = payload.get('action') or ''
        rec['actor'] = (payload.get('sender') or {}).get('login') or ''
        return rec
    if event == 'pull_request' and payload.get('pull_request'):
        it = payload['pull_request']
        it = dict(it)
        it['number'] = payload.get('number') or it.get('number')
        it['state'] = 'closed' if it.get('merged_at') else it.get('state')
        it['pull_request'] = {'url': it.get('url')}
        repo = (payload.get('repository') or {}).get('full_name', '')
        rec = normalize_issue(it, repo)
        rec['action'] = payload.get('action') or ''
        rec['actor'] = (payload.get('sender') or {}).get('login') or ''
        return rec
    return None
