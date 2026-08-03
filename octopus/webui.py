"""独立的手动主题分析推送页面（本地 Web 界面）。

启动：
    python main.py --manual-web                  # 默认 http://127.0.0.1:8765
    python main.py --manual-web --port 9000      # 换端口
    python main.py --manual-web --host 0.0.0.0   # 局域网/服务器使用（建议设令牌）

浏览器打开页面后：输入 AI 分析主题/内容 → 预览效果 → 发送推送。

与定时抓取流水线完全独立：
- 只做「人工录入 → 渲染 → 推送」，不经过时间校验与去重；
- 推送恒为**一对一**（PushPlus 个人推送），不携带群组 topic，
  与 config.yml 的 pushplus_topics（一对多）互不影响；
- 不依赖任何第三方 Web 框架，纯标准库 http.server。

安全：默认只监听本机 127.0.0.1。绑定 0.0.0.0 时请务必设置
环境变量 OCTOPUS_WEB_TOKEN，页面会要求输入访问令牌才能推送。
"""

from __future__ import annotations

import json
import logging
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

from .agent import Agent
from .render import render_manual
from .timeutil import now

log = logging.getLogger(__name__)

TOKEN = os.getenv("OCTOPUS_WEB_TOKEN", "").strip()

PAGE = """<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>章鱼 AI · 手动主题推送</title>
<style>
  body{margin:0;background:#eceff3;font-family:-apple-system,BlinkMacSystemFont,'PingFang SC','Helvetica Neue',Helvetica,Arial,sans-serif;color:#12305c;}
  .wrap{max-width:720px;margin:0 auto;padding:24px 16px 48px;}
  .card{background:#f6f7f9;border:1px solid #c9d3e0;border-left:5px solid #1d4f91;border-radius:8px;padding:16px 18px;margin-bottom:14px;}
  h1{font-size:20px;color:#0a1f3d;margin:0 0 4px;}
  .sub{font-size:12px;color:#3a5a86;margin-bottom:12px;line-height:1.6;}
  label{display:block;font-size:13px;font-weight:600;margin:10px 0 4px;}
  input[type=text],textarea{width:100%;box-sizing:border-box;border:1px solid #c9d3e0;border-radius:6px;padding:8px 10px;font-size:14px;color:#12305c;background:#fff;}
  textarea{min-height:200px;resize:vertical;line-height:1.65;}
  input[type=text]{height:38px;}
  .row{display:flex;gap:10px;margin-top:14px;}
  button{flex:1;border:0;border-radius:6px;padding:12px 0;font-size:15px;font-weight:600;cursor:pointer;}
  #btnPreview{background:#dde5f0;color:#1d4f91;}
  #btnPush{background:#12305c;color:#fff;}
  #status{font-size:13px;margin-top:10px;color:#3a5a86;min-height:18px;}
  #status.ok{color:#1b5e20;}
  #status.err{color:#b3261e;}
  .token-note{font-size:12px;color:#3a5a86;margin-top:8px;}
  .p-title{font-size:14px;font-weight:700;color:#0a1f3d;margin:14px 0 8px;}
  #preview{border:1px solid #c9d3e0;border-radius:8px;overflow:hidden;background:#eceff3;}
  #frame{width:100%;min-height:300px;border:0;background:#eceff3;display:block;}
</style>
</head>
<body>
<div class="wrap">
  <div class="card">
    <h1>章鱼 AI · 手动主题推送</h1>
    <div class="sub">一对一推送：直接发到 token 所属账号的微信，不走群组。<br>本页面与定时抓取完全独立，仅做「录入 → 预览 → 推送」。</div>
    <label for="topic">AI 分析主题（可选）</label>
    <input type="text" id="topic" placeholder="例如：机器人板块分析">
    <label for="content">AI 分析内容（必填）</label>
    <textarea id="content" placeholder="粘贴或输入 AI 分析全文，支持多行……"></textarea>
    <div class="row">
      <button id="btnPreview" type="button">预览效果</button>
      <button id="btnPush" type="button">发送推送</button>
    </div>
    <div id="status"></div>
    <div class="token-note" id="tokenNote" style="display:none;">此服务启用了访问令牌，推送前需填写：<br>
      <input type="text" id="token" placeholder="访问令牌（OCTOPUS_WEB_TOKEN）" style="height:34px;margin-top:6px;">
    </div>
  </div>
  <div class="p-title">推送预览（与微信收到的渲染效果一致）</div>
  <div id="preview"><iframe id="frame" title="预览"></iframe></div>
</div>
<script>
(function () {
  var AUTH = __AUTH__;
  if (AUTH) document.getElementById('tokenNote').style.display = 'block';
  function setStatus(text, cls) {
    var s = document.getElementById('status');
    s.textContent = text; s.className = cls || '';
  }
  function body() {
    var p = {
      topic: document.getElementById('topic').value,
      content: document.getElementById('content').value
    };
    var tk = document.getElementById('token');
    if (tk && tk.value) p.token = tk.value;
    return JSON.stringify(p);
  }
  function post(path, done) {
    fetch(path, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: body() })
      .then(function (r) { return r.json(); })
      .then(function (d) {
        if (!d.ok && d.message) { setStatus(d.message, 'err'); return; }
        done(d);
      })
      .catch(function (e) { setStatus('请求失败：' + e, 'err'); });
  }
  document.getElementById('btnPreview').addEventListener('click', function () {
    post('/preview', function (d) {
      document.getElementById('frame').srcdoc = d.html;
      setStatus('预览已更新', 'ok');
    });
  });
  document.getElementById('btnPush').addEventListener('click', function () {
    post('/push', function (d) {
      if (d.ok) setStatus('一对一推送成功：' + d.title, 'ok');
      else setStatus(d.message || '推送失败', 'err');
    });
  });
})();
</script>
</body>
</html>
"""


class ManualWebHandler(BaseHTTPRequestHandler):
    """处理手动推送页面的 GET/POST 请求。

    agent 由 serve_manual_web() 通过子类绑定，避免改签名。
    """

    server_version = "OctopusManual/1.0"
    agent: Agent

    # ------------------------------------------------------------------
    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path == "/":
            self._send_html(200, PAGE.replace("__AUTH__", "true" if TOKEN else "false"))
        elif path == "/healthz":
            self._send_json(200, {"ok": True, "auth": bool(TOKEN)})
        else:
            self._send_json(404, {"ok": False, "message": "页面不存在"})

    # ------------------------------------------------------------------
    def do_POST(self) -> None:
        path = urlparse(self.path).path
        if path not in ("/preview", "/push"):
            self._send_json(404, {"ok": False, "message": "接口不存在"})
            return

        try:
            body = self._read_json()
        except Exception as exc:  # noqa: BLE001 - 请求体坏了就给 400
            self._send_json(400, {"ok": False, "message": f"请求体解析失败：{exc}"})
            return

        if TOKEN and body.get("token") != TOKEN:
            self._send_json(401, {"ok": False, "message": "访问令牌不正确"})
            return

        topic = str(body.get("topic") or "").strip()
        content = str(body.get("content") or "").strip()
        if not content:
            self._send_json(400, {"ok": False, "message": "AI 分析内容不能为空"})
            return

        if path == "/preview":
            html = render_manual(topic, content, ref=now())
            self._send_json(200, {"ok": True, "html": html})
            return

        report = self.agent.push_manual(topic, content)
        self._send_json(200, {
            "ok": report.pushed,
            "title": report.title,
            "message": "推送成功" if report.pushed
            else "推送失败（请检查 PUSHPLUS_TOKEN 是否配置）",
        })

    # ------------------------------------------------------------------
    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        return json.loads(raw.decode("utf-8"))

    def _send_html(self, code: int, text: str) -> None:
        data = text.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _send_json(self, code: int, payload: dict) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, fmt: str, *args) -> None:  # 走应用日志，不刷 stderr
        log.info("%s %s", self.address_string(), fmt % args)


def serve_manual_web(agent: Agent, *, host: str = "127.0.0.1", port: int = 8765) -> int:
    """启动独立的手动推送页面服务，阻塞直到 Ctrl+C。返回进程退出码。"""
    handler = type("BoundManualWebHandler", (ManualWebHandler,), {"agent": agent})
    httpd = ThreadingHTTPServer((host, port), handler)
    actual_port = httpd.server_address[1]
    log.info("手动推送页面已启动：http://%s:%d/ （Ctrl+C 退出）", host, actual_port)
    if TOKEN:
        log.info("已启用访问令牌（OCTOPUS_WEB_TOKEN），页面需输入令牌才能推送")
    elif host in ("0.0.0.0", ""):
        log.warning("绑定 %s 且未设置 OCTOPUS_WEB_TOKEN，任何能访问该端口的人都能推送，请谨慎", host)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        log.info("手动推送页面已停止")
    finally:
        httpd.server_close()
    return 0
