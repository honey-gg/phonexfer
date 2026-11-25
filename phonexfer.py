#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
phonexfer —— 手机 ↔ 电脑 局域网双向传文件（零依赖，只用 Python 标准库）

用法
    phonexfer                          手机 → 电脑，存到 ~/Downloads/手机传入
    phonexfer recv -d ~/Desktop        手机 → 电脑，指定存放路径
    phonexfer send                     电脑 → 手机，把当前目录挂成下载页
    phonexfer send ~/Downloads         电脑 → 手机，指定要分享的目录
    phonexfer send model.gguf          电脑 → 手机，只分享一个文件

手机连同一个热点后，浏览器打开终端里打印的地址即可。
"""

import argparse
import html
import json
import os
import re
import socket
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import quote, unquote, urlparse, parse_qs

CHUNK = 1 << 18  # 256 KB
DEFAULT_RECV_DIR = "~/Downloads/手机传入"
DEFAULT_SEND_DIR = "~/Desktop"

BASE_CSS = """
:root{color-scheme:dark}
*{box-sizing:border-box;-webkit-tap-highlight-color:transparent}
body{margin:0;background:#141414;color:#ececec;
     font:16px/1.6 -apple-system,BlinkMacSystemFont,"PingFang SC","Helvetica Neue",sans-serif;
     padding:24px 16px 48px;max-width:640px;margin:0 auto}
h1{font-size:20px;font-weight:500;margin:0 0 4px}
.sub{color:#9a9a9a;font-size:14px;margin:0 0 24px}
.foot{margin-top:32px;font-size:12px;color:#6b6b6b;text-align:center;word-break:break-all}
.note{margin-top:24px;padding:12px 14px;background:#2a2113;border-radius:10px;
      font-size:13px;color:#ef9f27}
"""

ERROR_HTML = r"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<title>__TITLE__</title>
<style>__CSS__
.err{margin-top:20px;padding:16px;border-radius:12px;background:#2a1717;
     color:#f0a0a0;font-size:13px;word-break:break-all;line-height:1.7}
.err b{color:#f5c2c2;font-weight:500}
ol{margin:12px 0 0;padding-left:20px;color:#c9c9c9;font-size:14px}
ol li{margin:6px 0}
</style>
</head>
<body>
<h1>__TITLE__</h1>
<p class="sub">__SUB__</p>
__EXTRA__
<div class="foot">phonexfer</div>
</body>
</html>
"""


def error_page(title, sub, extra=""):
    """给手机端看的可读错误页（替代裸 JSON）。"""
    return (ERROR_HTML
            .replace("__CSS__", BASE_CSS)
            .replace("__TITLE__", html.escape(title))
            .replace("__SUB__", html.escape(sub))
            .replace("__EXTRA__", extra))


def readable(target):
    """探测路径能否被当前进程读取（macOS TCC 会在这里拦下 Downloads / Desktop）。"""
    try:
        if os.path.isdir(target):
            os.listdir(target)
        else:
            with open(target, "rb"):
                pass
        return True, ""
    except PermissionError as exc:
        return False, str(exc)
    except OSError as exc:
        return False, str(exc)

RECV_HTML = r"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<title>传到电脑</title>
<style>__CSS__
.pick{display:block;text-align:center;padding:20px;border:1.5px dashed #4a4a4a;border-radius:14px;
      background:#1c1c1c;font-size:17px;font-weight:500;color:#9fe1cb;cursor:pointer}
.pick:active{background:#242424}
body.drag .pick{border-color:#5dcaa5;background:#16302a}
input[type=file]{display:none}
.row{margin-top:16px;background:#1c1c1c;border-radius:12px;padding:14px}
.nm{font-size:14px;word-break:break-all;margin-bottom:10px}
.pg{height:6px;background:#2e2e2e;border-radius:3px;overflow:hidden}
.pg i{display:block;height:100%;width:0;background:#378add;transition:width .2s linear}
.pg i.ok{background:#1d9e75}
.pg i.err{background:#e24b4a}
.st{margin-top:8px;font-size:12px;color:#9a9a9a;font-variant-numeric:tabular-nums}
.mini{margin-left:8px;background:#1f3a33;color:#9fe1cb;border:0.5px solid #3d5c52;
      border-radius:7px;padding:3px 10px;font-size:12px}
.opts{display:flex;align-items:center;gap:10px;margin-top:16px;font-size:14px;color:#9a9a9a}
select{background:#1c1c1c;color:#ececec;border:1px solid #3a3a3a;border-radius:9px;
       padding:8px 10px;font-size:14px}
</style>
</head>
<body>
<h1>传到电脑</h1>
<p class="sub" id="sub">点下面的框选文件，或把文件拖进来</p>
<label class="pick" for="f">选择文件</label>
<input id="f" type="file" multiple>
<div class="opts">
  <span>限速</span>
  <select id="cap">
    <option value="0">不限速（最快）</option>
    <option value="30">30 MB/s</option>
    <option value="15">15 MB/s</option>
    <option value="8">8 MB/s（最凉最稳）</option>
  </select>
</div>
<div id="list"></div>
<div class="note" id="note" style="display:none">断点续传已开启：热点掉线或断网都不会白传，恢复后自动接着传，进度按已落盘字节算。</div>
<div class="foot">保存到：__SAVEDIR__</div>
<script>
var CHUNK = 8 * 1024 * 1024;
var input = document.getElementById('f');
var sub = document.getElementById('sub');
var note = document.getElementById('note');
var capSel = document.getElementById('cap');
var pending = [];
var running = false;

input.addEventListener('change', function () { queue(Array.prototype.slice.call(input.files)); input.value = ''; });
document.addEventListener('dragover', function (e) { e.preventDefault(); document.body.classList.add('drag'); });
document.addEventListener('dragleave', function () { document.body.classList.remove('drag'); });
document.addEventListener('drop', function (e) {
  e.preventDefault(); document.body.classList.remove('drag');
  queue(Array.prototype.slice.call(e.dataTransfer.files));
});

function queue(files) {
  if (!files.length) return;
  pending = pending.concat(files);
  note.style.display = 'block';
  if (!running) run();
}
function run() {
  running = true;
  sub.textContent = '正在传输…';
  var step = function () {
    if (!pending.length) { running = false; sub.textContent = '全部完成'; return; }
    upload(pending.shift()).then(step);
  };
  step();
}
function fmt(b) {
  if (b >= 1073741824) return (b / 1073741824).toFixed(2) + ' GB';
  if (b >= 1048576) return (b / 1048576).toFixed(1) + ' MB';
  if (b >= 1024) return (b / 1024).toFixed(0) + ' KB';
  return b + ' B';
}
function mmss(sec) {
  return Math.floor(sec / 60) + ' 分 ' + Math.floor(sec % 60) + ' 秒';
}

// 稳定的上传 id：同一个文件（名 + 大小 + 修改时间）永远算出同一个 id。
// 所以刷新页面、重开浏览器、甚至热点重连后换了 IP，都还能接上之前的进度。
function idOf(file) {
  var s = file.name + '|' + file.size + '|' + (file.lastModified || 0);
  var h = 2166136261;
  for (var i = 0; i < s.length; i++) { h ^= s.charCodeAt(i); h = (h * 16777619) >>> 0; }
  return 'u' + h.toString(36) + '_' + file.size.toString(36);
}

function upload(file) {
  return new Promise(function (done) {
    var id = idOf(file);
    var row = document.createElement('div');
    row.className = 'row';
    var nm = document.createElement('div'); nm.className = 'nm'; nm.textContent = file.name;
    var pg = document.createElement('div'); pg.className = 'pg'; var bar = document.createElement('i'); pg.appendChild(bar);
    var st = document.createElement('div'); st.className = 'st'; st.textContent = '准备中…';
    row.appendChild(nm); row.appendChild(pg); row.appendChild(st);
    document.getElementById('list').appendChild(row);

    var sent = 0, base = 0, t0 = Date.now(), tries = 0, ended = false;

    function show(bytes, extra) {
      var pct = file.size ? Math.min(100, bytes / file.size * 100) : 0;
      bar.style.width = pct.toFixed(1) + '%';
      var dt = (Date.now() - t0) / 1000;
      var sp = dt > 0.3 ? (bytes - base) / dt : 0;
      var left = sp > 1 ? Math.ceil((file.size - bytes) / sp) : 0;
      st.textContent = pct.toFixed(0) + '% · ' + fmt(bytes) + ' / ' + fmt(file.size) +
        (sp > 0 ? ' · ' + (sp / 1048576).toFixed(1) + ' MB/s' : '') +
        (left > 0 ? ' · 剩 ' + mmss(left) : '') + (extra ? ' · ' + extra : '');
    }
    function reset() { base = sent; t0 = Date.now(); }

    // 指数退避重试 —— 热点掉线后靠这一层自动爬回来，最长等 30 秒一轮
    function backoff() {
      if (ended) return;
      tries++;
      var wait = Math.min(30, Math.pow(1.5, tries));
      bar.className = '';
      show(sent, '连接中断，' + wait.toFixed(0) + ' 秒后自动续传（第 ' + tries + ' 次）');
      setTimeout(function () { probe(); }, wait * 1000);
    }

    // 续传前先问服务端：这个文件手上已经落了多少字节
    var askedStale = false;

    function probe(skipStale) {
      if (ended) return;
      var xhr = new XMLHttpRequest();
      xhr.open('GET', '/resume?id=' + id + '&name=' + encodeURIComponent(file.name) +
               '&size=' + file.size + '&_=' + Date.now());
      xhr.timeout = 10000;
      xhr.onload = function () {
        var j = {};
        try { j = JSON.parse(xhr.responseText); } catch (e) {}
        var off = j.offset || 0;
        if (off > sent) sent = off;
        if (!off && !skipStale && !askedStale && j.stale && j.stale.length) {
          askedStale = true;
          offerStale(j.stale[0]);
          return;
        }
        reset();
        if (sent >= file.size) { finish(); return; }
        show(sent, tries > 0 ? '已续传' : '');
        chunk();
      };
      xhr.onerror = backoff;
      xhr.ontimeout = backoff;
      xhr.send();
    }

    // 老版本（无分片时代）中断会留下一个「原名但没传完」的文件，问用户要不要接管
    function offerStale(info) {
      bar.style.width = '0%';
      st.textContent = '发现上次中断的残留「' + info.name + '」（' + fmt(info.size) + '）  ';
      var yes = document.createElement('button');
      yes.className = 'mini';
      yes.textContent = '接着传';
      yes.onclick = function () {
        st.textContent = '正在接管残留…';
        var x = new XMLHttpRequest();
        x.open('POST', '/adopt?id=' + id + '&name=' + encodeURIComponent(file.name) +
               '&size=' + file.size + '&from=' + encodeURIComponent(info.name));
        x.timeout = 120000;
        x.onload = function () {
          if (x.status === 200) { st.textContent = '已接管，继续上传…'; probe(true); }
          else { st.textContent = '接管失败（HTTP ' + x.status + '）'; }
        };
        x.onerror = function () { st.textContent = '接管失败（网络）'; };
        x.send();
      };
      st.appendChild(yes);
      var no = document.createElement('button');
      no.className = 'mini';
      no.textContent = '忽略，从头传';
      no.onclick = function () { probe(true); };
      st.appendChild(no);
    }

    function chunk() {
      if (ended) return;
      var start = sent;
      var end = Math.min(sent + CHUNK, file.size);
      var xhr = new XMLHttpRequest();
      xhr.open('POST', '/upload?name=' + encodeURIComponent(file.name) + '&id=' + id + '&size=' + file.size);
      xhr.setRequestHeader('Content-Type', 'application/octet-stream');
      xhr.setRequestHeader('X-Offset', String(start));
      xhr.timeout = 120000;
      if (xhr.upload) {
        xhr.upload.onprogress = function (e) {
          if (e.lengthComputable) show(start + e.loaded, '');
        };
      }
      xhr.onload = function () {
        if (xhr.status === 200) {
          // 以服务端回报的 offset 为准。万一这片被截断（服务端只收到一部分），
          // sent 会小于 end，下一轮自然把缺的那截补上，不会留下空洞。
          var got = end;
          try { got = JSON.parse(xhr.responseText).offset || end; } catch (e) {}
          if (got > end || got < start) got = end;   // 异常值就不采信
          sent = got;
          tries = 0;
          if (sent >= file.size) { finish(); return; }
          if (throttle()) return;
          chunk();
        } else {
          backoff();
        }
      };
      xhr.onerror = backoff;
      xhr.ontimeout = backoff;
      xhr.send(file.slice(start, end));
    }

    // 限速：按档位算「理论上该传到哪」，超前了就睡一会儿，给手机降温
    function throttle() {
      var cap = parseFloat(capSel.value) || 0;
      if (!cap) return false;
      var shouldTake = (sent - base) / (cap * 1048576) * 1000;
      var ahead = shouldTake - (Date.now() - t0);
      if (ahead > 40) { setTimeout(chunk, Math.min(ahead, 5000)); return true; }
      return false;
    }

    function finish() {
      if (ended) return;
      var xhr = new XMLHttpRequest();
      xhr.open('POST', '/complete?name=' + encodeURIComponent(file.name) + '&id=' + id + '&size=' + file.size);
      xhr.timeout = 60000;
      xhr.onload = function () {
        if (xhr.status === 200) {
          bar.style.width = '100%';
          bar.className = 'ok';
          st.textContent = '已保存 · ' + fmt(file.size);
          ended = true;
          done();
        } else {
          backoff();
        }
      };
      xhr.onerror = backoff;
      xhr.ontimeout = backoff;
      xhr.send();
    }

    probe();
  });
}
</script>
</body>
</html>
"""

SEND_HTML = r"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<title>从电脑下载</title>
<style>__CSS__
.item{display:flex;align-items:center;gap:14px;margin-top:10px;background:#1c1c1c;border-radius:12px;
      padding:14px 16px;color:inherit;text-decoration:none}
.item:active{background:#242424}
.meta{flex:1;min-width:0}
.nm{font-size:15px;word-break:break-all}
.sz{font-size:12px;color:#9a9a9a;margin-top:2px;font-variant-numeric:tabular-nums}
.go{flex:none;font-size:13px;color:#9fe1cb;border:0.5px solid #3d5c52;border-radius:8px;padding:5px 12px}
.item.dir .go{color:#b5d4f4;border-color:#2f4a63}
.crumb{margin:0 0 18px;font-size:13px;color:#9a9a9a;word-break:break-all}
.crumb a{color:#9fe1cb;text-decoration:none}
.empty{margin-top:24px;color:#7a7a7a;font-size:14px}
</style>
</head>
<body>
<h1>从电脑下载</h1>
<p class="crumb">__CRUMB__</p>
__ITEMS__
<div class="foot">__FOOT__</div>
</body>
</html>
"""

QR_HTML = r"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>扫码打开</title>
<style>
body{margin:0;min-height:100vh;background:#141414;color:#ececec;
     font:16px -apple-system,BlinkMacSystemFont,"PingFang SC",sans-serif;
     display:flex;flex-direction:column;align-items:center;justify-content:center;gap:18px}
#box{background:#fff;padding:14px;border-radius:14px;line-height:0;min-width:288px;min-height:288px;
     display:flex;align-items:center;justify-content:center;color:#999;font-size:13px}
code{font-size:19px;color:#9fe1cb;font-variant-numeric:tabular-nums}
.tip{font-size:13px;color:#8a8a8a}
</style>
</head>
<body>
<div id="box">二维码加载中…</div>
<code>__URL__</code>
<div class="tip">用手机相机 / 微信扫一扫打开</div>
<script src="https://cdn.jsdelivr.net/npm/qrcode@1.5.3/build/qrcode.min.js"></script>
<script>
try {
  QRCode.toCanvas(document.createElement('canvas'), '__URL__', { width: 260, margin: 0 }, function (err, cv) {
    if (!err) { var b = document.getElementById('box'); b.textContent = ''; b.appendChild(cv); }
  });
} catch (e) {}
</script>
</body>
</html>
"""


# --------------------------------------------------------------------------
# 公共工具
# --------------------------------------------------------------------------

def detect_lan_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("223.5.5.5", 80))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


def human(n):
    if n >= 1 << 30:
        return "%.2f GB" % (n / (1 << 30))
    if n >= 1 << 20:
        return "%.1f MB" % (n / (1 << 20))
    if n >= 1 << 10:
        return "%.0f KB" % (n / (1 << 10))
    return "%d B" % n


def clean_name(raw):
    name = os.path.basename((raw or "").replace("\\", "/"))
    name = re.sub(r"[\x00-\x1f/]", "", name).strip()
    return "unnamed" if name in ("", ".", "..") else name


def unique_path(directory, raw_name):
    name = clean_name(raw_name)
    stem, ext = os.path.splitext(name)
    target = os.path.join(directory, name)
    n = 1
    while os.path.exists(target):
        target = os.path.join(directory, "%s(%d)%s" % (stem, n, ext))
        n += 1
    return target


class Hub:
    """把服务端事件转发给前端（GUI 用）。CLI 下 hub 为 None，行为完全不变。"""

    def __init__(self, sink):
        self.sink = sink

    def log(self, msg):
        self.sink(("log", msg))

    def progress(self, name, done, total, rate):
        self.sink(("progress", name, done, total, rate))

    def finished(self, name, nbytes, secs):
        self.sink(("finished", name, nbytes, secs))

    def failed(self, name, err):
        self.sink(("failed", name, err))


class Base(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "phonexfer/1.0"
    hub = None

    def log_message(self, fmt, *args):
        sys.stderr.write("[%s] %s\n" % (time.strftime("%H:%M:%S"), fmt % args))

    def _send(self, code, body, ctype):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code, obj):
        self._send(code, json.dumps(obj, ensure_ascii=False),
                   "application/json; charset=utf-8")

    def _body(self):
        if "chunked" in (self.headers.get("Transfer-Encoding") or "").lower():
            return self._chunked()
        return self._fixed(int(self.headers.get("Content-Length") or 0))

    def _fixed(self, total):
        left = total
        while left > 0:
            data = self.rfile.read(min(CHUNK, left))
            if not data:
                return
            left -= len(data)
            yield data

    def _chunked(self):
        while True:
            line = self.rfile.readline().strip()
            if b";" in line:
                line = line.split(b";")[0]
            try:
                size = int(line, 16)
            except ValueError:
                return
            if size == 0:
                self.rfile.readline()
                return
            left = size
            while left > 0:
                data = self.rfile.read(min(CHUNK, left))
                if not data:
                    return
                left -= len(data)
                yield data
            self.rfile.readline()

    def _qr(self):
        page = QR_HTML.replace("__CSS__", "").replace("__URL__", html.escape(self.lan_url))
        self._send(200, page, "text/html; charset=utf-8")


# --------------------------------------------------------------------------
# recv：手机 → 电脑
# --------------------------------------------------------------------------

class RecvHandler(Base):
    save_dir = "."
    lan_url = ""

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        if path in ("/", "/index.html"):
            page = (RECV_HTML.replace("__CSS__", BASE_CSS)
                            .replace("__SAVEDIR__", html.escape(self.save_dir)))
            self._send(200, page, "text/html; charset=utf-8")
        elif path == "/qr":
            self._qr()
        elif path == "/health":
            self._json(200, {"ok": True, "mode": "recv", "dir": self.save_dir})
        elif path == "/resume":
            # 续传前先问：这个文件手上已经落了多少字节
            qs = parse_qs(parsed.query)
            uid = self._uid(qs)
            name = clean_name(unquote((qs.get("name") or [""])[0]))
            try:
                total = int((qs.get("size") or ["0"])[0])
            except ValueError:
                total = 0
            offset = 0
            have_part = False
            if uid:
                part, _ = self._part_files(uid)
                if os.path.exists(part):
                    have_part = True
                    try:
                        offset = os.path.getsize(part)
                    except OSError:
                        offset = 0
            reply = {"ok": True, "offset": offset}
            if not have_part and name:
                # 老版本（无分片时代）中断会留下一个「原名但没传完」的文件，
                # 它不是 .part，新协议接不上。这里只把候选报给前端，由用户点确认。
                stale = self._stale_candidates(name, total)
                if stale:
                    reply["stale"] = [{"name": n, "size": s} for s, n in stale]
            self._json(200, reply)
        else:
            self._json(404, {"ok": False, "error": "not found"})

    def do_POST(self):
        parsed = urlparse(self.path)
        qs = parse_qs(parsed.query)
        if parsed.path == "/upload":
            uid = self._uid(qs)
            if uid:
                self._receive_chunk(uid, qs)
            else:
                self._receive_plain(qs)
        elif parsed.path == "/complete":
            self._complete(self._uid(qs), qs)
        elif parsed.path == "/adopt":
            self._adopt(self._uid(qs), qs)
        else:
            self._json(404, {"ok": False, "error": "not found"})

    # ---------------------------------------------------------- 分片续传
    @property
    def part_dir(self):
        return os.path.join(self.save_dir, ".phonexfer-parts")

    def _uid(self, qs):
        raw = (qs.get("id") or [""])[0]
        return re.sub(r"[^A-Za-z0-9_-]", "", raw)[:64]

    def _part_files(self, uid):
        return (os.path.join(self.part_dir, uid + ".part"),
                os.path.join(self.part_dir, uid + ".name"))

    def _stale_candidates(self, name, total):
        """找保存目录里「同名、且比目标小」的文件 —— 多半是老版本中断留下的残留。"""
        if total <= 0 or not name:
            return []
        stem, ext = os.path.splitext(name)
        found = []
        try:
            entries = os.listdir(self.save_dir)
        except OSError:
            return []
        for fn in entries:
            base, fext = os.path.splitext(fn)
            if fext != ext:
                continue
            if base != stem and not (base.startswith(stem + "(") and base.endswith(")")):
                continue
            p = os.path.join(self.save_dir, fn)
            if not os.path.isfile(p):
                continue
            try:
                sz = os.path.getsize(p)
            except OSError:
                continue
            if 0 < sz < total:          # 比目标小才可能是半成品；完整文件绝不动
                found.append((sz, fn))
        found.sort(reverse=True)
        return found

    def _adopt(self, uid, qs):
        """把老版本残留的半成品接管成分片，让续传能从它继续。用户点了确认才会走到这里。"""
        if not uid:
            self._json(400, {"ok": False, "error": "missing id"})
            return
        src_name = clean_name(unquote((qs.get("from") or [""])[0]))
        name = clean_name(unquote((qs.get("name") or [""])[0])) or src_name
        try:
            total = int((qs.get("size") or ["0"])[0])
        except ValueError:
            total = 0
        src = os.path.join(self.save_dir, src_name)   # clean_name 只留 basename，无穿越风险
        if not os.path.isfile(src):
            self._json(404, {"ok": False, "error": "残留文件不存在"})
            return
        size = os.path.getsize(src)
        if size <= 0 or (total and size >= total):
            self._json(409, {"ok": False, "error": "这个文件不该被当成半成品"})
            return
        part, metaf = self._part_files(uid)
        if os.path.exists(part):
            self._json(409, {"ok": False, "error": "已经有分片了"})
            return
        os.makedirs(self.part_dir, exist_ok=True)
        try:
            os.replace(src, part)
        except OSError as exc:
            self._json(500, {"ok": False, "error": str(exc)})
            return
        try:
            with open(metaf, "w", encoding="utf-8") as fh:
                fh.write(name)
        except OSError:
            pass
        self._json(200, {"ok": True, "offset": size, "took": src_name})
        self.log_message("接管残留 %s（%.1f MB）", src_name, size / 1048576.0)
        if self.hub is not None:
            self.hub.log("已接管上次中断的残留「%s」（%s），从断点继续"
                         % (src_name, human(size)))

    def _receive_chunk(self, uid, qs):
        name = clean_name(unquote((qs.get("name") or ["unnamed"])[0]))
        try:
            total = int((qs.get("size") or ["0"])[0])
        except ValueError:
            total = 0
        os.makedirs(self.part_dir, exist_ok=True)
        part, metaf = self._part_files(uid)
        if not os.path.exists(metaf):
            try:
                with open(metaf, "w", encoding="utf-8") as fh:
                    fh.write(name)
            except OSError:
                pass

        try:
            offset = int(self.headers.get("X-Offset") or 0)
        except ValueError:
            offset = 0
        have = os.path.getsize(part) if os.path.exists(part) else 0

        if offset > have:
            # 客户端以为传得更多，但服务端没那么多字节，让它回退重问
            self._json(409, {"ok": False, "error": "offset ahead",
                             "offset": have})
            return

        try:
            if offset == have:
                fh = open(part, "ab")
            else:
                fh = open(part, "r+b")   # 回退：截掉多出来的，从 offset 重写
                fh.seek(offset)
                fh.truncate()
        except OSError as exc:
            self._json(500, {"ok": False, "error": str(exc)})
            return

        if offset > 0 and self.hub is not None:
            self.hub.log("续传 %s 从 %s 继续" % (name, human(offset)))

        written = 0
        started = time.time()
        tick = started
        try:
            with fh:
                for data in self._body():
                    fh.write(data)
                    written += len(data)
                    if self.hub is not None:
                        now = time.time()
                        if now - tick >= 0.15:
                            tick = now
                            self.hub.progress(name, offset + written, total,
                                              written / max(now - started, 1e-6))
        except Exception as exc:  # noqa: BLE001
            self._json(500, {"ok": False, "error": str(exc)})
            if self.hub is not None:
                self.hub.failed(name, str(exc))
            return

        got = offset + written
        self._json(200, {"ok": True, "offset": got, "received": written})
        if self.hub is not None and written:
            self.hub.progress(name, got, total,
                              written / max(time.time() - started, 1e-6))

    def _complete(self, uid, qs):
        if not uid:
            self._json(400, {"ok": False, "error": "missing id"})
            return
        part, metaf = self._part_files(uid)
        if not os.path.exists(part):
            self._json(404, {"ok": False, "error": "no such upload"})
            return

        name = clean_name(unquote((qs.get("name") or [""])[0]))
        if not name and os.path.exists(metaf):
            try:
                with open(metaf, encoding="utf-8") as fh:
                    name = clean_name(fh.read())
            except OSError:
                pass
        if not name:
            name = "unnamed"

        size = os.path.getsize(part)
        try:
            expect = int((qs.get("size") or ["0"])[0])
        except ValueError:
            expect = 0
        if expect and size != expect:
            self._json(409, {"ok": False, "error": "size mismatch",
                             "have": size, "want": expect})
            return

        target = unique_path(self.save_dir, name)
        started = time.time()
        try:
            os.replace(part, target)
        except OSError as exc:
            self._json(500, {"ok": False, "error": str(exc)})
            return
        try:
            os.remove(metaf)
        except OSError:
            pass

        fname = os.path.basename(target)
        self._json(200, {"ok": True, "saved": target, "bytes": size})
        self.log_message("已保存 %s  (%.1f MB)", fname, size / 1048576.0)
        if self.hub is not None:
            self.hub.progress(fname, size, size, 0.0)
            self.hub.finished(fname, size, max(time.time() - started, 1e-6))

    # ------------------------------------- 老式整文件直传（curl 等不带 id）
    def _receive_plain(self, qs):
        raw = (qs.get("name") or ["unnamed"])[0]
        target = unique_path(self.save_dir, unquote(raw))
        fname = os.path.basename(target)
        try:
            expected = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            expected = 0
        if self.hub is not None:
            self.hub.progress(fname, 0, expected, 0.0)
        written = 0
        started = time.time()
        tick = started
        try:
            with open(target, "wb") as fh:
                for data in self._body():
                    fh.write(data)
                    written += len(data)
                    if self.hub is not None:
                        now = time.time()
                        if now - tick >= 0.15:
                            tick = now
                            self.hub.progress(fname, written, expected,
                                              written / max(now - started, 1e-6))
        except Exception as exc:  # noqa: BLE001
            self._json(500, {"ok": False, "error": str(exc)})
            self.log_message("写入失败 %s: %s", target, exc)
            if self.hub is not None:
                self.hub.failed(fname, str(exc))
            return
        if expected and written != expected:
            # 关键：连接中途断掉时 rfile.read() 返回空，循环会「静默」结束。
            # 老版本据此就当成成功、写下短文件（这就是差 100MB 的根因）。
            self._json(400, {"ok": False, "error": "incomplete",
                             "got": written, "want": expected})
            self.log_message("传输不完整 %s：收到 %.1f MB / 应为 %.1f MB（已保留为残留，可续传）",
                             fname, written / 1048576.0, expected / 1048576.0)
            if self.hub is not None:
                self.hub.failed(fname, "传输不完整：%d / %d 字节" % (written, expected))
            return

        elapsed = max(time.time() - started, 1e-6)
        mb = written / 1048576.0
        self._json(200, {"ok": True, "saved": target, "bytes": written})
        self.log_message("已保存 %s  (%.1f MB, %.1f MB/s)", fname, mb, mb / elapsed)
        if self.hub is not None:
            self.hub.progress(fname, written, written, mb / elapsed)
            self.hub.finished(fname, written, elapsed)


# --------------------------------------------------------------------------
# send：电脑 → 手机
# --------------------------------------------------------------------------

class SendHandler(Base):
    root = "."
    lan_url = ""

    # ---- 路径安全：只允许访问 root 之下的内容 ----
    def _resolve(self, rel):
        rel = unquote(rel or "")
        root = os.path.realpath(self.root)
        target = os.path.realpath(os.path.join(root, rel.lstrip("/")))
        if target != root and not target.startswith(root + os.sep):
            return None
        return target

    def do_GET(self):
        parsed = urlparse(self.path)
        route = parsed.path
        qs = parse_qs(parsed.query)
        if route == "/qr":
            self._qr()
            return
        if route == "/health":
            self._json(200, {"ok": True, "mode": "send", "root": self.root})
            return
        if route == "/f":
            target = self._resolve((qs.get("p") or [""])[0])
            if target and os.path.isfile(target):
                self._serve_file(target)
            else:
                self._json(404, {"ok": False, "error": "file not found"})
            return
        target = self._resolve((qs.get("p") or [""])[0])
        if target is None:
            self._json(403, {"ok": False, "error": "forbidden"})
            return
        if os.path.isfile(target):
            self._serve_file(target)
            return
        self._listing(target, (qs.get("p") or [""])[0])

    def _listing(self, directory, rel):
        root = os.path.realpath(self.root)
        root_name = os.path.basename(root) or root
        rel = rel.strip("/")
        try:
            names = sorted(os.listdir(directory),
                           key=lambda s: (not os.path.isdir(os.path.join(directory, s)), s.lower()))
        except PermissionError:
            self._send(200, error_page(
                "电脑没有授权访问这个文件夹",
                "手机端收到的是系统权限拦截，不是网络问题。",
                '<div class="err"><b>%s</b></div>'
                '<ol><li>回到 Mac，在「系统设置 → 隐私与安全性 → 文件与文件夹」里，'
                '允许「手机互传 / Python」访问 <b>下载</b> 或 <b>桌面</b>。</li>'
                '<li>或者挑一个不受保护的位置重新分享，例如「文稿」下的自建目录。</li>'
                '<li>改完在 App 里点「停止」再「启动」。</li></ol>'
                % html.escape(directory)), "text/html; charset=utf-8")
            return
        except OSError as exc:
            self._send(200, error_page(
                "打不开这个文件夹",
                "电脑端读取失败。",
                '<div class="err">%s</div>' % html.escape(str(exc))),
                "text/html; charset=utf-8")
            return

        rows = []
        if rel:
            parent = os.path.dirname(rel)
            rows.append(
                '<a class="item dir" href="/?p=%s"><div class="meta">'
                '<div class="nm">..</div><div class="sz">返回上级</div></div>'
                '<div class="go">打开</div></a>' % quote(parent))
        total = 0
        for name in names:
            if name.startswith("."):
                continue
            full = os.path.join(directory, name)
            child = (rel + "/" + name) if rel else name
            try:
                is_dir = os.path.isdir(full)
                size = 0 if is_dir else os.path.getsize(full)
            except OSError:
                continue
            total += size
            if is_dir:
                rows.append(
                    '<a class="item dir" href="/?p=%s"><div class="meta">'
                    '<div class="nm">%s</div><div class="sz">文件夹</div></div>'
                    '<div class="go">打开</div></a>' % (quote(child), html.escape(name)))
            else:
                rows.append(
                    '<a class="item" href="/f?p=%s"><div class="meta">'
                    '<div class="nm">%s</div><div class="sz">%s</div></div>'
                    '<div class="go">下载</div></a>'
                    % (quote(child), html.escape(name), human(size)))

        crumb = ('<a href="/">%s</a>' % html.escape(root_name)) if rel else html.escape(root_name)
        if rel:
            parts = rel.split("/")
            acc = []
            for i, seg in enumerate(parts):
                acc.append(seg)
                if i < len(parts) - 1:
                    crumb += ' / <a href="/?p=%s">%s</a>' % (quote("/".join(acc)), html.escape(seg))
                else:
                    crumb += " / " + html.escape(seg)
            crumb += ' &nbsp;·&nbsp; <a href="/?p=%s">返回上级</a>' % quote(os.path.dirname(rel))

        body = "".join(rows) if rows else '<div class="empty">这个文件夹是空的</div>'
        page = (SEND_HTML.replace("__CSS__", BASE_CSS)
                         .replace("__CRUMB__", crumb)
                         .replace("__ITEMS__", body)
                         .replace("__FOOT__", "共 %d 项 · %s" % (len(rows), human(total))))
        self._send(200, page, "text/html; charset=utf-8")

    def _serve_file(self, path):
        fname = os.path.basename(path)
        try:
            size = os.path.getsize(path)
        except OSError:
            self._json(404, {"ok": False, "error": "not found"})
            return

        start, end, code = 0, size - 1, 200
        rng = self.headers.get("Range") or ""
        if rng.startswith("bytes="):
            spec = rng[6:].split(",")[0].strip()
            a, _, b = spec.partition("-")
            try:
                if a:
                    start = int(a)
                    if b:
                        end = int(b)
                elif b:
                    start = max(0, size - int(b))
                if start > end or start >= size:
                    raise ValueError
                code = 206
            except ValueError:
                self.send_response(416)
                self.send_header("Content-Range", "bytes */%d" % size)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return

        length = end - start + 1
        self.send_response(code)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(length))
        self.send_header("Accept-Ranges", "bytes")
        if code == 206:
            self.send_header("Content-Range", "bytes %d-%d/%d" % (start, end, size))
        self.send_header("Content-Disposition",
                         "attachment; filename=download; filename*=UTF-8''%s" % quote(fname))
        self.end_headers()

        started = time.time()
        tick = started
        sent = 0
        if self.hub is not None:
            self.hub.progress(fname, 0, length, 0.0)
        try:
            with open(path, "rb") as fh:
                fh.seek(start)
                left = length
                while left > 0:
                    data = fh.read(min(CHUNK, left))
                    if not data:
                        break
                    self.wfile.write(data)
                    left -= len(data)
                    sent = length - left
                    if self.hub is not None:
                        now = time.time()
                        if now - tick >= 0.15:
                            tick = now
                            self.hub.progress(fname, sent, length,
                                              sent / max(now - started, 1e-6))
        except (BrokenPipeError, ConnectionResetError):
            self.log_message("客户端中断：%s", fname)
            if self.hub is not None:
                self.hub.failed(fname, "客户端中断")
            return
        except OSError as exc:
            self.log_message("读取失败：%s (%s)", fname, exc)
            if self.hub is not None:
                self.hub.failed(fname, "读取失败：%s" % exc)
            return
        elapsed = max(time.time() - started, 1e-6)
        mb = length / 1048576.0
        self.log_message("已发送 %s  (%.1f MB, %.1f MB/s)", fname, mb, mb / elapsed)
        if self.hub is not None:
            self.hub.progress(fname, length, length, mb / elapsed)
            self.hub.finished(fname, length, elapsed)


# --------------------------------------------------------------------------
# 入口
# --------------------------------------------------------------------------

def bind_server(host, port, handler, tries=11):
    last = None
    for offset in range(tries):
        try:
            srv = ThreadingHTTPServer((host, port + offset), handler)
            srv.daemon_threads = True
            return srv, port + offset
        except OSError as exc:
            last = exc
    raise SystemExit("端口 %d-%d 都被占用：%s" % (port, port + tries - 1, last))


def create_server(mode, target, host, port=8000, hub=None, tries=11):
    """CLI 与 GUI 共用的建服务函数。返回 (server, 实际使用的端口)。"""
    target = os.path.abspath(os.path.expanduser(target))
    if mode == "recv":
        os.makedirs(target, exist_ok=True)
        handler = RecvHandler
        handler.save_dir = target
    else:
        if not os.path.exists(target):
            raise ValueError("路径不存在：%s" % target)
        ok, why = readable(target)
        if not ok:
            raise ValueError(
                "电脑没有权限读取这个位置：\n%s\n\n"
                "macOS 把「下载」「桌面」这类目录保护起来了，需要在\n"
                "「系统设置 → 隐私与安全性 → 文件与文件夹」里\n"
                "允许「手机互传」访问它，然后重开 App 再试。\n\n"
                "也可以换一个不受保护的位置，比如「文稿」下的目录。\n\n"
                "系统原话：%s" % (target, why))
        handler = SendHandler
        handler.root = target
    handler.hub = hub
    srv, actual = bind_server(host, port, handler, tries)
    shown = detect_lan_ip() if host in ("0.0.0.0", "", None) else host
    handler.lan_url = "http://%s:%d/" % (shown, actual)
    return srv, actual


def main():
    ap = argparse.ArgumentParser(
        prog="phonexfer",
        description="手机 ↔ 电脑 局域网双向传文件（零依赖）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""示例
  phonexfer                          手机 → 电脑，存到 ~/Downloads/手机传入
  phonexfer recv -d ~/Desktop        手机 → 电脑，存到桌面
  phonexfer send                     电脑 → 手机，挂载当前目录
  phonexfer send ~/Downloads -p 9000 电脑 → 手机，指定目录与端口
""")
    ap.add_argument("mode", nargs="?", default="recv", choices=["recv", "send"],
                    help="recv=手机传到电脑（默认），send=电脑传到手机")
    ap.add_argument("target", nargs="?", default=None,
                    help="send 模式下要分享的文件或目录（等价于 -d）")
    ap.add_argument("-d", "--dir", default=None,
                    help="recv 的保存目录 / send 的分享目录")
    ap.add_argument("-p", "--port", type=int, default=8000, help="端口（默认 8000，被占用自动 +1）")
    ap.add_argument("-H", "--host", default=None, help="绑定地址（默认自动识别局域网 IP）")
    ap.add_argument("-o", "--open", action="store_true", help="启动后在电脑上打开二维码页")
    args = ap.parse_args()

    ip = args.host or detect_lan_ip()

    if args.mode == "recv":
        target_dir = os.path.abspath(os.path.expanduser(args.dir or DEFAULT_RECV_DIR))
    else:
        target_dir = os.path.abspath(os.path.expanduser(
            args.dir or args.target or DEFAULT_SEND_DIR))
        if not os.path.exists(target_dir):
            raise SystemExit("路径不存在：%s" % target_dir)

    try:
        srv, port = create_server(args.mode, target_dir, ip, args.port)
    except ValueError as exc:
        raise SystemExit(str(exc))
    handler_cls = RecvHandler if args.mode == "recv" else SendHandler

    line = "=" * 58
    title = "手机 → 电脑（接收）" if args.mode == "recv" else "电脑 → 手机（下载）"
    print(line)
    print("  phonexfer · %s" % title)
    print(line)
    print("  手机浏览器打开：  %s" % handler_cls.lan_url)
    print("  电脑上看二维码：  http://%s:%d/qr" % (ip, port))
    if args.mode == "recv":
        print("  保存目录：        %s" % target_dir)
    else:
        print("  分享路径：        %s" % target_dir)
    print(line)
    print("  手机需连同一个热点。按 Ctrl+C 停止。")
    print()
    sys.stdout.flush()

    if args.open:
        os.system("open 'http://%s:%d/qr' >/dev/null 2>&1" % (ip, port))

    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止。")
    finally:
        srv.server_close()


if __name__ == "__main__":
    main()
