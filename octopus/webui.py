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
<title>C:\\OCTOPUS\\MANUAL.EXE · 章鱼 AI 推送台</title>
<style>
  /* DOS / CRT 监视器皮肤：黑屏、磷光绿、等宽字、直角窗口 + 标题栏。
     页面不固定尺寸：宽度 100% 跟随设备，高度由内容撑开。 */
  :root{
    --ink:#c8f0c8;--bright:#ffffff;--dim:#63a86f;--faint:#3f7a4c;
    --phos:#3dff82;--border:#1f7a3c;--soft:#123f22;--panel:#0b120c;
    --bar:#0f2a17;--glass:#05100a;--amber:#ffb000;--red:#ff5f5f;
  }
  *{box-sizing:border-box;}
  html,body{margin:0;padding:0;background:#000;}
  body{
    color:var(--ink);font-size:15px;line-height:1.75;letter-spacing:.2px;
    font-family:'Courier New',Courier,'Lucida Console','NSimSun','SimSun',monospace;
    text-shadow:0 0 3px rgba(61,255,130,.35);padding:16px 13px 56px;
  }
  /* 扫描线 + 四角暗角：整块屏幕的滤镜，压在内容下层，不影响点选 */
  body::before{
    content:"";position:fixed;inset:0;pointer-events:none;z-index:99;
    background:
      radial-gradient(125% 125% at 50% 50%,rgba(0,0,0,0) 55%,rgba(0,0,0,.7) 100%),
      repeating-linear-gradient(180deg,rgba(0,0,0,.45) 0px,rgba(0,0,0,.45) 1px,rgba(0,0,0,0) 1px,rgba(0,0,0,0) 3px);
  }
  .wrap{width:100%;}
  .win{background:var(--panel);border:1px solid var(--border);overflow:hidden;
       margin-bottom:16px;box-shadow:inset 0 0 26px rgba(0,0,0,.75),0 0 12px rgba(61,255,130,.12);}
  .bar{display:flex;justify-content:space-between;align-items:center;gap:10px;
        background:var(--bar);border-bottom:1px solid var(--border);padding:4px 8px;
        font-size:12px;letter-spacing:.8px;color:#7dffa8;font-weight:700;}
  .bar .btns span{display:inline-block;background:#7dffa8;color:#070b08;padding:0 5px;margin-left:4px;font-size:10px;}
  .pad{padding:14px 15px 16px;}
  h1{font-size:26px;line-height:1.3;margin:0;color:var(--bright);letter-spacing:1px;}
  h1 .dot{color:var(--phos);}
  .sub{font-size:13px;color:var(--dim);margin-top:8px;line-height:1.8;}
  label{display:block;font-size:12px;letter-spacing:1px;color:var(--faint);margin:16px 0 5px;}
  input[type=text],textarea{width:100%;background:var(--glass);border:1px solid var(--border);
    border-radius:0;color:var(--bright);padding:9px 11px;
    font-family:inherit;font-size:15px;line-height:1.7;text-shadow:0 0 3px rgba(61,255,130,.3);}
  input[type=text]:focus,textarea:focus{outline:0;border-color:var(--phos);
    box-shadow:0 0 0 1px var(--phos),0 0 14px rgba(61,255,130,.25);}
  textarea{min-height:38vh;resize:vertical;line-height:1.8;}
  .row{display:flex;gap:10px;flex-wrap:wrap;margin-top:16px;}
  button{flex:1 1 180px;background:#12331f;color:#7dffa8;border:1px solid var(--border);
    border-radius:0;font-family:inherit;font-size:15px;font-weight:700;letter-spacing:1px;
    padding:11px 0;cursor:pointer;
    box-shadow:inset 2px 2px 0 rgba(125,255,168,.22),inset -2px -2px 0 rgba(0,0,0,.6);}
  button:hover{background:var(--phos);color:#03130a;}
  button:active{box-shadow:inset -2px -2px 0 rgba(125,255,168,.2),inset 2px 2px 0 rgba(0,0,0,.6);}
  #btnPush{background:var(--phos);color:#03130a;}
  #btnPush:hover{background:#7dffa8;}
  #status{font-size:13px;margin-top:12px;min-height:20px;color:var(--dim);white-space:pre-wrap;}
  #status.ok{color:var(--phos);}
  #status.err{color:var(--red);}
  .token-note{font-size:12px;color:var(--amber);margin-top:10px;line-height:1.7;}
  .p-title{font-size:12px;letter-spacing:1px;color:var(--faint);margin:20px 2px 8px;}
  /* 预览窗：宽度跟随窗口，高度按正文自动撑开，不再锁死成 300×400 */
  #preview{width:100%;background:#070b08;border:1px solid var(--border);
    box-shadow:inset 0 0 26px rgba(0,0,0,.8),0 0 14px rgba(61,255,130,.12);}
  #frame{width:100%;height:auto;min-height:40vh;border:0;display:block;background:#070b08;}
  .cursor{display:inline-block;width:9px;height:15px;background:var(--phos);
    vertical-align:-2px;margin-left:5px;animation:blink 1.1s steps(1,end) infinite;}
  @keyframes blink{50%{opacity:0}}
</style>
</head>
<body>
<div class="wrap">
  <div class="win">
    <div class="bar"><span>C:\\OCTOPUS\\MANUAL.EXE</span><span class="btns"><span>_</span><span>□</span><span>×</span></span></div>
    <div class="pad">
      <h1><span class="dot">█</span> 章鱼 AI 全景分析</h1>
      <div class="sub">全网 AI 调研境内境外数据，由多个大模型混合部署。<br>这里用于录入正文、预览微信长页效果，并一对一发送到 token 所属账号，不走群组。</div>
      <label for="topic">&gt; 分析主题（可选）</label>
      <input type="text" id="topic" placeholder="例如：AI 应用全景、机器人板块、海外模型动态">
      <label for="content">&gt; 分析正文（必填）</label>
      <textarea id="content" placeholder="粘贴或输入分析全文；预览与推送都会排成自适应宽度的 DOS 终端页……"></textarea>
      <div class="row">
        <button id="btnPreview" type="button">F2 · 预览效果</button>
        <button id="btnPush" type="button">F9 · 发送推送</button>
      </div>
      <div id="status"></div>
      <div class="token-note" id="tokenNote" style="display:none;">此服务启用了访问令牌，推送前需填写：<br>
        <input type="text" id="token" placeholder="访问令牌（OCTOPUS_WEB_TOKEN）" style="margin-top:6px;">
      </div>
    </div>
  </div>
  <div class="p-title">&gt; 推送预览（与微信收到的渲染效果一致）<span class="cursor"></span></div>
  <div id="preview"><iframe id="frame" title="预览"></iframe></div>
</div>
<script>
(function () {
  var AUTH = __AUTH__;
  if (AUTH) document.getElementById('tokenNote').style.display = 'block';
  var frame = document.getElementById('frame');
  function setStatus(text, cls) {
    var s = document.getElementById('status');
    s.textContent = text; s.className = cls || '';
  }
  function fit() {  // 预览窗高度跟着正文走，不裁内容
    try {
      var doc = frame.contentDocument || frame.contentWindow.document;
      var h = Math.max(doc.body.scrollHeight, doc.documentElement.scrollHeight) + 24;
      frame.style.height = h + 'px';
    } catch (e) { /* 跨域读不到就算了，min-height 兜着 */ }
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
        if (!d.ok && d.message) { setStatus('! ' + d.message, 'err'); return; }
        done(d);
      })
      .catch(function (e) { setStatus('! 请求失败：' + e, 'err'); });
  }
  function preview() {
    post('/preview', function (d) {
      frame.removeAttribute('srcdoc');
      frame.srcdoc = d.html;
      frame.onload = fit;
      setTimeout(fit, 60);
      setStatus('OK 预览已更新', 'ok');
    });
  }
  function push() {
    post('/push', function (d) {
      if (d.ok) setStatus('OK 一对一推送成功：' + d.title, 'ok');
      else setStatus('! ' + (d.message || '推送失败'), 'err');
    });
  }
  document.getElementById('btnPreview').addEventListener('click', preview);
  document.getElementById('btnPush').addEventListener('click', push);
  document.addEventListener('keydown', function (e) {
    if (e.key === 'F2') { e.preventDefault(); preview(); }
    if (e.key === 'F9') { e.preventDefault(); push(); }
  });
  setStatus('C:\\\\OCTOPUS> manual.exe /READY  （F2 预览 · F9 推送）', 'ok');
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
            html = self.agent.preview_manual(topic, content, ref=now())
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
