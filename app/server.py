#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Digital Me · 单进程服务
一个进程同时提供：
- 静态页面（app/ 目录）
- /proxy 同源 API 代理（浏览器 -> /proxy -> 真实 API，解决 CORS）
- /api/config 后端模型配置（key 不下发到浏览器，访问者免配置直接对话）

模型配置来源（优先级）：环境变量 > server-config.json > 默认值
  DM_BASE / DM_KEY / DM_MODEL / DM_MAX_TOKENS / DM_TEMP

用法: python3 app/server.py  然后访问 http://localhost:5616
端口可用环境变量 PORT 覆盖，监听地址 HOST 覆盖（容器内需 0.0.0.0）。
"""
import http.server
import urllib.request
import urllib.error
import json
import os
import time
import urllib.parse
import admin_db

BASE = os.path.dirname(os.path.abspath(__file__))
PORT = int(os.environ.get('PORT', '5616'))
HOST = os.environ.get('HOST', '127.0.0.1')  # Docker 容器内需设 0.0.0.0
CONFIG_FILE = os.path.join(BASE, 'server-config.json')

SESSION_COOKIE = 'dm_admin'

# 跨域白名单：仅允许博客域名跨域调用 /proxy 与 /api/config（聊天功能接入博客用）
# 空 Origin（同源/curl）不受限；不在白名单的 Origin 不返回 CORS 头，浏览器会拦截
CORS_ORIGINS = (
    # 如果你要把聊天窗口嵌入博客，在这里添加博客的域名
    # 本地开发调试（保留）
    'http://localhost:4173',
    'http://localhost:5173',
    'http://127.0.0.1:4173',
    'http://127.0.0.1:5173',
)

DEFAULTS = {
    'base': 'https://api.deepseek.com/v1',
    'key': '',
    'model': 'deepseek-chat',
    'max_tokens': 2000,
    'temp': 0.9,
}


def load_config():
    cfg = dict(DEFAULTS)
    for k in ('DM_BASE', 'DM_KEY', 'DM_MODEL', 'DM_MAX_TOKENS', 'DM_TEMP'):
        v = os.environ.get(k)
        if v:
            cfg[k.lower().replace('dm_', '')] = v
    try:
        if os.path.exists(CONFIG_FILE):
            # 多方案格式（presets+active）交给 dm_admin 解析，兼容旧版单方案
            fc = json.load(open(CONFIG_FILE, encoding='utf-8'))
            if isinstance(fc.get('presets'), list) and fc['presets']:
                rc = admin_db.read_config()
                for k in DEFAULTS:
                    if rc.get(k):
                        cfg[k] = rc[k]
            else:
                for k in DEFAULTS:
                    if fc.get(k):
                        cfg[k] = fc[k]
    except Exception:
        pass
    cfg['max_tokens'] = int(cfg['max_tokens'])
    cfg['temp'] = float(cfg['temp'])
    return cfg


def _extract_answer(full_body, content_type=''):
    """从代理响应中提取 AI 回答文本。
    支持两种格式：
    1. SSE 流式：每行 'data: {...}'，content 在 choices[0].delta.content（火山/DeepSeek）
    2. 普通 JSON：choices[0].message.content
    """
    try:
        text = full_body.decode('utf-8', errors='ignore')
    except Exception:
        return ''
    # 1) SSE 流式：行内含 'data: ' 即按流式解析
    if 'data: ' in text:
        parts = []
        for line in text.splitlines():
            line = line.strip()
            if not line.startswith('data:'):
                continue
            payload = line[5:].strip()
            if not payload or payload == '[DONE]':
                continue
            try:
                j = json.loads(payload)
            except Exception:
                continue
            choices = j.get('choices') or []
            if not choices:
                continue
            delta = choices[0].get('delta') or {}
            # 优先取 content；reasoning_content 是思考过程，不作为回答
            c = delta.get('content')
            if c is None:
                c = delta.get('message', {}).get('content') if isinstance(delta, dict) else None
            if c:
                parts.append(c)
        if parts:
            return ''.join(parts)
    # 2) 普通 JSON
    try:
        j = json.loads(text)
        return (j.get('choices') or [{}])[0].get('message', {}).get('content', '') or ''
    except Exception:
        return ''


class Handler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *a, **kw):
        super().__init__(*a, directory=BASE, **kw)

    def end_headers(self):
        # 禁用缓存：保证数据文件更新后浏览器立即拿到新版（避免陈旧 data.js）
        self.send_header('Cache-Control', 'no-cache, no-store, must-revalidate')
        # 跨域支持：仅对公开聊天接口(/proxy /api/config)放行白名单来源，管理接口不开放跨域
        path = urllib.parse.urlparse(self.path).path
        if path in ('/proxy', '/api/config'):
            origin = self.headers.get('Origin', '')
            if origin in CORS_ORIGINS:
                self.send_header('Access-Control-Allow-Origin', origin)
                self.send_header('Access-Control-Allow-Credentials', 'true')
                self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
                self.send_header('Access-Control-Allow-Headers', 'Content-Type, Authorization, X-Target-Base')
                self.send_header('Access-Control-Max-Age', '86400')
        super().end_headers()

    def do_OPTIONS(self):
        # CORS 预检请求：直接 204 放行（end_headers 已带上允许头）
        self.send_response(204)
        self.end_headers()

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path

        # ---- 后台管理路由 ----
        if path == '/login':
            self._serve_login()
            return
        if path == '/logout':
            self._clear_session()
            self.send_response(302)
            self.send_header('Location', '/login')
            self.end_headers()
            return
        if path == '/admin':
            if not self._require_admin():
                return
            self._serve_file('admin.html')
            return
        if path == '/api/admin/stats':
            if not self._require_admin():
                return
            self._send_json(admin_db.query_stats())
            return
        if path == '/api/admin/conversations':
            if not self._require_admin():
                return
            qs = urllib.parse.parse_qs(parsed.query)
            page = int(qs.get('page', ['1'])[0] or 1)
            per = int(qs.get('per_page', ['10'])[0] or 10)
            kw = qs.get('keyword', [''])[0]
            prov = qs.get('province', [''])[0]
            city = qs.get('city', [''])[0]
            self._send_json(admin_db.query_conversations(page, per, kw, prov, city))
            return
        if path == '/api/admin/regions':
            if not self._require_admin():
                return
            self._send_json(admin_db.query_regions())
            return
        if path == '/api/admin/conversations/<int:cid>' or path.startswith('/api/admin/conversations/'):
            self.send_error(405)
            return
        if path == '/api/admin/config':
            if not self._require_admin():
                return
            c = admin_db.read_config()
            # key 脱敏：只回传是否有 key，不泄露完整 key
            c['has_key'] = bool(c.get('key'))
            c['key'] = ''
            c['presets'] = admin_db.list_presets()
            self._send_json(c)
            return
        if path == '/api/admin/presets':
            if not self._require_admin():
                return
            self._send_json({'presets': admin_db.list_presets()})
            return

        if path == '/api/config':
            c = load_config()
            body = json.dumps({
                'base': c['base'],
                'model': c['model'],
                'max_tokens': c['max_tokens'],
                'temp': c['temp'],
                'server_key': bool(c['key']),
            }, ensure_ascii=False).encode('utf-8')
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(body)
            return
        super().do_GET()

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)

        # ---- 管理员登录 ----
        if parsed.path == '/login':
            self._handle_login()
            return

        # ---- 修改密码 ----
        if parsed.path == '/api/admin/change_password':
            if not self._require_admin():
                return
            self._handle_change_password()
            return

        # ---- 保存模型配置 ----
        if parsed.path == '/api/admin/config':
            if not self._require_admin():
                return
            self._handle_save_config()
            return

        # ---- 方案管理 ----
        if parsed.path == '/api/admin/presets/add':
            if not self._require_admin():
                return
            self._handle_preset_add()
            return
        if parsed.path == '/api/admin/presets/delete':
            if not self._require_admin():
                return
            self._handle_preset_delete()
            return
        if parsed.path == '/api/admin/presets/activate':
            if not self._require_admin():
                return
            self._handle_preset_activate()
            return
        if parsed.path == '/api/admin/presets/test':
            if not self._require_admin():
                return
            self._handle_preset_test()
            return

        if self.path != '/proxy':
            self.send_error(404)
            return
        c = load_config()
        question = ''
        start_ts = time.time()
        try:
            length = int(self.headers.get('Content-Length', 0))
            body = self.rfile.read(length) if length else b''
            # 请求体参数兜底：客户端缺省/为空时用后端配置（server-config.json）
            try:
                bj = json.loads(body) if body else {}
                if not bj.get('model'):
                    bj['model'] = c['model']
                if not bj.get('max_tokens'):
                    bj['max_tokens'] = c['max_tokens']
                if bj.get('temperature') is None:
                    bj['temperature'] = c['temp']
                # 人格注入：无 system 消息时，自动构建系统提示词（PERSONA/年鉴/画像/检索）
                msgs = bj.get('messages') or []
                if isinstance(msgs, list) and not any(
                        isinstance(m, dict) and m.get('role') == 'system' for m in msgs):
                    try:
                        import persona
                        last_user = ''
                        for m in reversed(msgs):
                            if isinstance(m, dict) and m.get('role') == 'user':
                                last_user = m.get('content', '')
                                break
                        question = last_user
                        # 页面感知：前端传来的当前页面信息（博客文章页 title/desc）
                        pg = bj.get('page') if isinstance(bj.get('page'), dict) else None
                        bj['messages'] = persona.inject_system(msgs, last_user, pg)
                        # 记录本次检索命中的记忆类型（用于前端「参考了 XX 记忆」标签）
                        try:
                            _refs = [r.get('type') for r in persona.search(last_user, 8) if r.get('type')]
                            _refs = list(dict.fromkeys(_refs))[:4]
                        except Exception:
                            _refs = []
                    except Exception as e:
                        print('[dm] 人格注入失败:', e)
                        _refs = []
                else:
                    _refs = []
                body = json.dumps(bj, ensure_ascii=False).encode('utf-8')
            except Exception:
                pass
            target = (self.headers.get('X-Target-Base') or '').strip() or c['base']
            url = target.rstrip('/') + '/chat/completions'
            req = urllib.request.Request(url, data=body, method='POST')
            req.add_header('Content-Type', 'application/json')
            auth = self.headers.get('Authorization', '')
            if not auth and c['key']:
                auth = 'Bearer ' + c['key']
            if auth:
                req.add_header('Authorization', auth)
            with urllib.request.urlopen(req, timeout=180) as resp:
                self.send_response(resp.status)
                self.send_header('Content-Type', resp.headers.get('Content-Type', 'application/json'))
                self.end_headers()
                full = b''
                while True:
                    chunk = resp.read(4096)
                    if not chunk:
                        break
                    full += chunk
                    self.wfile.write(chunk)
                    self.wfile.flush()
                # 流式响应末尾追加检索记忆类型（前端「参考了 XX 记忆」标签）
                # 注意：只有 SSE 流式才追加；普通 JSON 响应不追加（避免破坏响应体）
                try:
                    is_stream = (resp.headers.get('Content-Type', '') or '').find('text/event-stream') >= 0
                    if is_stream and _refs:
                        extra = '\ndata: ' + json.dumps(
                            {'dm_refs': _refs}, ensure_ascii=False) + '\n\n'
                        self.wfile.write(extra.encode('utf-8'))
                        self.wfile.flush()
                except Exception as e:
                    print('[dm] 追加 refs 失败:', e)
                # 记录对话（流式结束后一次性写库，不影响前端体验）
                try:
                    answer = _extract_answer(full, resp.headers.get('Content-Type', ''))
                    admin_db.log_conversation(
                        self, question, answer,
                        duration_ms=int((time.time() - start_ts) * 1000), ok=True)
                except Exception as e:
                    print('[dm] 日志记录失败:', e)
        except urllib.error.HTTPError as e:
            self.send_response(e.code)
            self.end_headers()
            self.wfile.write(e.read())
            try:
                admin_db.log_conversation(
                    self, question, 'HTTP {}'.format(e.code),
                    duration_ms=int((time.time() - start_ts) * 1000), ok=False)
            except Exception:
                pass
        except Exception as e:
            self.send_response(502)
            self.end_headers()
            self.wfile.write(str(e).encode('utf-8'))
            try:
                admin_db.log_conversation(
                    self, question, str(e)[:500],
                    duration_ms=int((time.time() - start_ts) * 1000), ok=False)
            except Exception:
                pass

    def do_DELETE(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        if path.startswith('/api/admin/conversations/'):
            if not self._require_admin():
                return
            try:
                cid = int(path.rsplit('/', 1)[1])
            except (ValueError, IndexError):
                self.send_error(400)
                return
            admin_db.delete_conversation(cid)
            self._send_json({'ok': True, 'deleted': cid})
            return
        self.send_error(404)

    def log_message(self, *a):
        pass

    # ---------------- 后台辅助 ----------------

    def _get_cookie(self):
        raw = self.headers.get('Cookie', '')
        for part in raw.split(';'):
            part = part.strip()
            if part.startswith(SESSION_COOKIE + '='):
                return part[len(SESSION_COOKIE) + 1:]
        return ''

    def _require_admin(self):
        token = self._get_cookie()
        if admin_db.validate_session(token):
            return True
        self.send_response(302)
        self.send_header('Location', '/login')
        self.end_headers()
        return False

    def _set_session_cookie(self, token):
        self.send_header('Set-Cookie', '{}={}; Path=/; HttpOnly; SameSite=Lax'.format(SESSION_COOKIE, token))

    def _clear_session(self):
        admin_db.destroy_session(self._get_cookie())
        self.send_header('Set-Cookie', '{}=; Path=/; HttpOnly; Max-Age=0'.format(SESSION_COOKIE))

    def _handle_login(self):
        length = int(self.headers.get('Content-Length', 0))
        body = self.rfile.read(length) if length else b''
        try:
            form = urllib.parse.parse_qs(body.decode('utf-8'))
            user = (form.get('username') or [''])[0]
            pw = (form.get('password') or [''])[0]
        except Exception:
            user = pw = ''
        if user and pw and admin_db.check_login(user, pw):
            token = admin_db.create_session(user)
            self.send_response(302)
            self._set_session_cookie(token)
            self.send_header('Location', '/admin')
            self.end_headers()
        else:
            self.send_response(302)
            self.send_header('Location', '/login?error=1')
            self.end_headers()

    def _handle_change_password(self):
        """修改管理员密码（JSON：{old_password, new_password}）"""
        length = int(self.headers.get('Content-Length', 0))
        body = self.rfile.read(length) if length else b''
        try:
            bj = json.loads(body.decode('utf-8')) if body else {}
            old_pw = bj.get('old_password', '')
            new_pw = bj.get('new_password', '')
        except Exception:
            self._send_json({'ok': False, 'msg': '请求格式错误'})
            return
        ok, msg = admin_db.change_password(
            admin_db.validate_session(self._get_cookie()), old_pw, new_pw)
        self._send_json({'ok': ok, 'msg': msg})

    def _handle_save_config(self):
        """保存模型配置（JSON：{base, key, model, max_tokens, temp, preset_name?}）"""
        length = int(self.headers.get('Content-Length', 0))
        body = self.rfile.read(length) if length else b''
        try:
            bj = json.loads(body.decode('utf-8')) if body else {}
        except Exception:
            self._send_json({'ok': False, 'msg': '请求格式错误'})
            return
        cur = admin_db.read_config()
        # key 留空则保持原值（不回传完整 key 的安全策略）
        if not bj.get('key'):
            bj['key'] = cur.get('key', '')
        ok, msg = admin_db.save_config(bj)
        self._send_json({'ok': ok, 'msg': msg})

    def _handle_preset_add(self):
        length = int(self.headers.get('Content-Length', 0))
        body = self.rfile.read(length) if length else b''
        try:
            bj = json.loads(body.decode('utf-8')) if body else {}
        except Exception:
            self._send_json({'ok': False, 'msg': '请求格式错误'})
            return
        ok, msg = admin_db.add_preset(
            bj.get('name', '').strip(),
            bj.get('base', '').strip(),
            bj.get('key', '').strip(),
            bj.get('model', '').strip())
        self._send_json({'ok': ok, 'msg': msg})

    def _handle_preset_delete(self):
        length = int(self.headers.get('Content-Length', 0))
        body = self.rfile.read(length) if length else b''
        try:
            bj = json.loads(body.decode('utf-8')) if body else {}
        except Exception:
            self._send_json({'ok': False, 'msg': '请求格式错误'})
            return
        ok, msg = admin_db.delete_preset(bj.get('name', ''))
        self._send_json({'ok': ok, 'msg': msg})

    def _handle_preset_activate(self):
        length = int(self.headers.get('Content-Length', 0))
        body = self.rfile.read(length) if length else b''
        try:
            bj = json.loads(body.decode('utf-8')) if body else {}
        except Exception:
            self._send_json({'ok': False, 'msg': '请求格式错误'})
            return
        ok, msg = admin_db.activate_preset(bj.get('name', ''))
        self._send_json({'ok': ok, 'msg': msg})

    def _handle_preset_test(self):
        """测试方案连接：用给定 base/key/model 发一个最小请求验证连通性"""
        length = int(self.headers.get('Content-Length', 0))
        body = self.rfile.read(length) if length else b''
        try:
            bj = json.loads(body.decode('utf-8')) if body else {}
        except Exception:
            self._send_json({'ok': False, 'msg': '请求格式错误'})
            return
        base = (bj.get('base') or '').strip()
        key = (bj.get('key') or '').strip()
        model = (bj.get('model') or '').strip()
        if not base:
            self._send_json({'ok': False, 'msg': '请先填写 API Base URL'})
            return
        if not model:
            self._send_json({'ok': False, 'msg': '请先填写模型名'})
            return
        # key 为空时用当前配置的 key
        if not key:
            key = admin_db.read_config().get('key', '')
        url = base.rstrip('/') + '/chat/completions'
        payload = json.dumps({
            'model': model,
            'messages': [{'role': 'user', 'content': 'hi'}],
            'max_tokens': 5,
        }).encode('utf-8')
        req = urllib.request.Request(url, data=payload, method='POST')
        req.add_header('Content-Type', 'application/json')
        if key:
            req.add_header('Authorization', 'Bearer ' + key)
        start_ts = time.time()
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                body_resp = resp.read(2000)
            ms = int((time.time() - start_ts) * 1000)
            try:
                j = json.loads(body_resp.decode('utf-8', errors='ignore'))
                if 'error' in j:
                    self._send_json({'ok': False, 'msg': 'API 错误: {}'.format(j['error'])})
                    return
                self._send_json({'ok': True, 'msg': '连接成功（{}ms）'.format(ms), 'ms': ms})
            except Exception:
                self._send_json({'ok': True, 'msg': '连接成功（{}ms）'.format(ms), 'ms': ms})
        except urllib.error.HTTPError as e:
            try:
                err = e.read(500).decode('utf-8', errors='ignore')
            except Exception:
                err = ''
            self._send_json({'ok': False, 'msg': 'HTTP {}: {}'.format(e.code, err[:200])})
        except Exception as e:
            self._send_json({'ok': False, 'msg': '连接失败: {}'.format(str(e)[:200])})

    def _serve_login(self):
        """登录页：优先读本地 login.html，找不到时返回内嵌页"""
        p = os.path.join(BASE, 'login.html')
        if os.path.exists(p):
            self._serve_file('login.html')
            return
        html = '''<!DOCTYPE html><html lang="zh"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Digital Me 后台登录</title><style>
body{font-family:-apple-system,sans-serif;background:#f5f6f8;display:flex;align-items:center;justify-content:center;min-height:100vh;margin:0}
.card{background:#fff;border-radius:16px;padding:40px 44px;box-shadow:0 8px 30px rgba(0,0,0,.08);width:340px}
h1{font-size:20px;margin:0 0 6px} p.sub{color:#888;font-size:13px;margin:0 0 24px}
label{display:block;font-size:13px;color:#555;margin:14px 0 6px}
input{width:100%;box-sizing:border-box;padding:10px 12px;border:1px solid #ddd;border-radius:8px;font-size:14px}
button{width:100%;margin-top:22px;padding:11px;background:#0a7cf0;color:#fff;border:0;border-radius:8px;font-size:15px;cursor:pointer}
button:hover{background:#0968c8} .err{color:#e23;font-size:13px;margin-top:12px;text-align:center}
</style></head><body><form class="card" method="post" action="/login">
<h1>Digital Me 后台</h1><p class="sub">请登录以查看对话记录</p>
<label>用户名</label><input name="username" autocomplete="username" required>
<label>密码</label><input name="password" type="password" autocomplete="current-password" required>
<button type="submit">登 录</button>
<div class="err">''' + ('账号或密码错误' if 'error=1' in self.path else '') + '''</div>
</form></body></html>'''
        data = html.encode('utf-8')
        self.send_response(200)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _serve_file(self, name):
        p = os.path.join(BASE, name)
        if not os.path.exists(p):
            self.send_error(404)
            return
        data = open(p, 'rb').read()
        ctype = 'text/html; charset=utf-8' if name.endswith('.html') else 'application/octet-stream'
        self.send_response(200)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _send_json(self, obj):
        body = json.dumps(obj, ensure_ascii=False).encode('utf-8')
        self.send_response(200)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)


if __name__ == '__main__':
    c = load_config()
    print(f'Digital Me 服务已启动 -> http://{HOST}:{PORT}  (模型: {c["model"]} @ {c["base"]})')
    print(f'后端密钥已配置: {"是" if c["key"] else "否（需前端填 key 或设置环境变量）"}')
    http.server.ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()
