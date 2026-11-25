#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""手机 ⇄ 电脑 互传 —— 图形界面版

复用 phonexfer.py 的引擎，套一层 tkinter 界面：
可切换「接收 / 发送」、可选路径、显示二维码与实时速度。
"""

import os
import queue
import re
import subprocess
import sys
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import phonexfer  # noqa: E402

try:
    from tkinterdnd2 import DND_FILES, TkinterDnD
    HAS_DND = True
except Exception:
    HAS_DND = False

try:
    import qrcode
    HAS_QR = True
except Exception:
    HAS_QR = False


def human(n):
    if n >= 1 << 30:
        return "%.2f GB" % (n / (1 << 30))
    if n >= 1 << 20:
        return "%.1f MB" % (n / (1 << 20))
    if n >= 1 << 10:
        return "%.0f KB" % (n / (1 << 10))
    return "%d B" % n


def parse_drop(raw):
    """解析 tkdnd 的拖拽数据：'{含空格路径} 裸路径'"""
    out = []
    for a, b in re.findall(r"\{([^}]*)\}|(\S+)", raw or ""):
        p = a or b
        if p:
            out.append(p)
    return out


class App:
    def __init__(self):
        self.root = TkinterDnD.Tk() if HAS_DND else tk.Tk()
        self.root.title("手机 ⇄ 电脑 互传")
        self.root.geometry("600x790")
        self.root.minsize(540, 720)

        self.server = None
        self.thread = None
        self.url = ""
        self.q = queue.Queue()
        self.last_name = ""
        self.indet = False
        self.user_picked = False

        self.mode = tk.StringVar(value="recv")
        self.path_var = tk.StringVar(value=os.path.expanduser(phonexfer.DEFAULT_RECV_DIR))
        self.port_var = tk.StringVar(value="8000")
        self.status_var = tk.StringVar(value="未启动")
        self.rate_var = tk.StringVar(value="")
        self.url_var = tk.StringVar(value="—")

        self._build()
        self._sync_mode()
        self._pump()

        if HAS_DND:
            self.root.drop_target_register(DND_FILES)
            self.root.dnd_bind("<<Drop>>", self._on_drop)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    # ------------------------------------------------------------------ UI
    def _build(self):
        pad = {"padx": 18}
        root = self.root

        head = ttk.Frame(root)
        head.pack(fill="x", pady=(16, 10), **pad)
        ttk.Label(head, text="手机 ⇄ 电脑 互传", font=("", 17, "bold")).pack(anchor="w")
        ttk.Label(
            head,
            text="手机连同一个热点，浏览器打开下方地址即可（支持拖拽、断点续传）",
            foreground="#777",
        ).pack(anchor="w", pady=(3, 0))

        # 模式
        box = ttk.LabelFrame(root, text=" 模式 ")
        box.pack(fill="x", pady=6, **pad)
        ttk.Radiobutton(box, text="接收 · 手机 → 电脑", value="recv",
                        variable=self.mode, command=self._sync_mode).pack(anchor="w", padx=14, pady=(8, 2))
        ttk.Radiobutton(box, text="发送 · 电脑 → 手机", value="send",
                        variable=self.mode, command=self._sync_mode).pack(anchor="w", padx=14, pady=(0, 10))

        # 路径
        pbox = ttk.LabelFrame(root, text=" 路径 ")
        pbox.pack(fill="x", pady=6, **pad)
        row = ttk.Frame(pbox)
        row.pack(fill="x", padx=14, pady=(8, 4))
        self.path_entry = ttk.Entry(row, textvariable=self.path_var)
        self.path_entry.pack(side="left", fill="x", expand=True)
        btns = ttk.Frame(pbox)
        btns.pack(fill="x", padx=14, pady=(0, 10))
        ttk.Button(btns, text="选择文件夹…", command=self._pick_dir).pack(side="left")
        ttk.Button(btns, text="选择文件…", command=self._pick_file).pack(side="left", padx=8)
        self.hint = ttk.Label(btns, text="", foreground="#777")
        self.hint.pack(side="left", padx=6)

        # 端口
        obox = ttk.Frame(root)
        obox.pack(fill="x", pady=(4, 0), **pad)
        ttk.Label(obox, text="端口").pack(side="left")
        ttk.Entry(obox, textvariable=self.port_var, width=8).pack(side="left", padx=8)
        ttk.Label(obox, text="被占用会自动换下一个", foreground="#777").pack(side="left")

        # 主按钮
        self.toggle = ttk.Button(root, text="启 动", command=self._toggle)
        self.toggle.pack(fill="x", pady=12, **pad)

        # 连接信息
        self.info = ttk.LabelFrame(root, text=" 手机访问地址 ")
        self.info.pack(fill="x", pady=6, **pad)
        inner = ttk.Frame(self.info)
        inner.pack(fill="x", padx=14, pady=10)

        self.qr_canvas = tk.Canvas(inner, width=170, height=170, bg="white",
                                   highlightthickness=1, highlightbackground="#ddd")
        self.qr_canvas.pack(side="left")

        right = ttk.Frame(inner)
        right.pack(side="left", fill="both", expand=True, padx=(16, 0))
        ttk.Label(right, text="在手机浏览器里打开：", foreground="#777").pack(anchor="w")
        self.url_label = ttk.Label(right, textvariable=self.url_var, font=("Menlo", 12),
                                   wraplength=300, justify="left")
        self.url_label.pack(anchor="w", pady=(4, 10))
        brow = ttk.Frame(right)
        brow.pack(anchor="w")
        ttk.Button(brow, text="复制地址", command=self._copy).pack(side="left")
        self.open_btn = ttk.Button(brow, text="打开目标位置", command=self._open_target, state="disabled")
        self.open_btn.pack(side="left", padx=8)

        # 进度
        pbar = ttk.Frame(root)
        pbar.pack(fill="x", pady=(10, 0), **pad)
        self.bar = ttk.Progressbar(pbar, mode="determinate", maximum=100)
        self.bar.pack(fill="x")
        line = ttk.Frame(root)
        line.pack(fill="x", pady=(6, 0), **pad)
        ttk.Label(line, textvariable=self.status_var).pack(side="left")
        ttk.Label(line, textvariable=self.rate_var, foreground="#2a7", font=("Menlo", 12)).pack(side="right")

        # 日志
        lbox = ttk.LabelFrame(root, text=" 日志 ")
        lbox.pack(fill="both", expand=True, pady=10, **pad)
        self.log = tk.Text(lbox, height=7, wrap="none", relief="flat", background="#fafafa")
        sb = ttk.Scrollbar(lbox, orient="vertical", command=self.log.yview)
        self.log.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self.log.pack(side="left", fill="both", expand=True, padx=(8, 0), pady=8)
        self.log.configure(state="disabled")

        self._log("就绪。选好模式与路径后点「启动」。")

    # -------------------------------------------------------------- helpers
    def _log(self, msg):
        self.log.configure(state="normal")
        self.log.insert("end", msg + "\n")
        self.log.see("end")
        self.log.configure(state="disabled")

    def _sync_mode(self):
        recv = self.mode.get() == "recv"
        self.hint.configure(text="保存到这里" if recv else "把这个文件夹/文件分享出去")
        recv_dflt = os.path.expanduser(phonexfer.DEFAULT_RECV_DIR)
        send_dflt = os.path.expanduser(getattr(phonexfer, "DEFAULT_SEND_DIR", "~/Desktop"))
        if not os.path.isdir(send_dflt):
            send_dflt = os.path.expanduser("~")
        cur = self.path_var.get().strip()

        # 用户自己选过的路径不动；没选过就按模式给默认值。
        # 以前切到「发送」时仍沿用接收目录，会分享一个跟用户无关的空文件夹。
        if not self.user_picked or cur in (recv_dflt, send_dflt):
            self.path_var.set(recv_dflt if recv else send_dflt)
        self._set_info(False)

    def _pick_dir(self):
        p = filedialog.askdirectory(title="选择文件夹")
        if p:
            self.path_var.set(p)
            self.user_picked = True

    def _pick_file(self):
        p = filedialog.askopenfilename(title="选择文件")
        if not p:
            return
        self.user_picked = True
        if self.mode.get() == "recv":
            self.path_var.set(os.path.dirname(p))
            self._log("接收模式下已改用该文件所在文件夹：%s" % os.path.dirname(p))
        else:
            self.path_var.set(p)

    def _on_drop(self, event):
        paths = parse_drop(event.data)
        if not paths:
            return
        p = paths[0]
        self.user_picked = True
        if os.path.isfile(p):
            self.mode.set("send")
            self._sync_mode()
            self.path_var.set(p)
        else:
            self.path_var.set(p)
        self._log("已选择：%s" % p)
        self._log("已选择：%s" % p)

    def _copy(self):
        if not self.url:
            return
        self.root.clipboard_clear()
        self.root.clipboard_append(self.url)
        self._log("地址已复制到剪贴板")

    def _open_target(self):
        target = os.path.expanduser(self.path_var.get().strip() or "")
        if not target:
            return
        if os.path.isfile(target):
            subprocess.Popen(["open", "-R", target])
        else:
            subprocess.Popen(["open", target])

    def _set_info(self, on):
        self.open_btn.configure(state="normal" if on else "disabled")

    # ---------------------------------------------------------------- 服务
    def _toggle(self):
        if self.server is None:
            self._start()
        else:
            self._stop()

    def _start(self):
        mode = self.mode.get()
        target = self.path_var.get().strip()
        if not target:
            target = phonexfer.DEFAULT_RECV_DIR if mode == "recv" else os.getcwd()
            self.path_var.set(target)
        target = os.path.abspath(os.path.expanduser(target))
        try:
            port = int(self.port_var.get().strip() or "8000")
            if not (1 <= port <= 65535):
                raise ValueError
        except ValueError:
            messagebox.showerror("端口不对", "端口要是 1–65535 的数字")
            return

        ip = phonexfer.detect_lan_ip()
        if ip.startswith("127."):
            messagebox.showwarning("没有局域网",
                                   "没检测到局域网地址，请确认已连上手机热点。")
            return

        hub = phonexfer.Hub(self.q.put)
        try:
            srv, actual = phonexfer.create_server(mode, target, ip, port, hub=hub)
        except ValueError as exc:
            messagebox.showerror("这个位置用不了", str(exc))
            return
        except (OSError, SystemExit) as exc:
            messagebox.showerror(
                "端口被占用",
                "从 %d 开始试了 11 个端口都被占用了。\n换个端口再试。\n\n%s" % (port, exc))
            return

        cls = phonexfer.RecvHandler if mode == "recv" else phonexfer.SendHandler
        self.url = cls.lan_url
        self.server = srv
        self.thread = threading.Thread(target=srv.serve_forever, daemon=True)
        self.thread.start()

        self.port_var.set(str(actual))
        self.url_var.set(self.url)
        self._draw_qr(self.url)
        self.toggle.configure(text="停 止")
        self.status_var.set("等待手机连接…")
        self.rate_var.set("")
        self.bar.stop()
        self.bar.configure(mode="determinate")
        self.bar["value"] = 0
        self.indet = False
        self._set_info(True)
        self._log("已启动 · %s · %s" % ("接收" if mode == "recv" else "发送", target))
        self._log("地址 %s" % self.url)
        if mode == "send":
            try:
                visible = [x for x in os.listdir(target) if not x.startswith(".")] \
                    if os.path.isdir(target) else [target]
            except OSError:
                visible = []
            if not visible:
                self._log("注意：这个位置是空的，手机打开只会看到空列表。"
                          "要传文件请先「选择文件…」或把文件拖进窗口。")

    def _stop(self):
        srv, self.server = self.server, None
        self.toggle.configure(text="启 动")
        self.status_var.set("已停止")
        self.rate_var.set("")
        self.url_var.set("—")
        self.url = ""
        self.qr_canvas.delete("all")
        self._set_info(False)
        self._log("已停止")
        if srv is None:
            return

        def down():
            try:
                srv.shutdown()
                srv.server_close()
            except Exception:
                pass

        threading.Thread(target=down, daemon=True).start()

    def _on_close(self):
        srv, self.server = self.server, None
        if srv is not None:
            try:
                srv.shutdown()
            except Exception:
                pass
        self.root.destroy()

    # ------------------------------------------------------------- 二维码
    def _draw_qr(self, text):
        c = self.qr_canvas
        c.delete("all")
        if not HAS_QR:
            c.create_text(85, 85, text="二维码库缺失\n请用上方地址", width=150,
                          fill="#888", justify="center")
            return
        try:
            qr = qrcode.QRCode(border=2, box_size=1)
            qr.add_data(text)
            qr.make(fit=True)
            m = qr.get_matrix()
        except Exception as exc:
            c.create_text(85, 85, text="二维码生成失败", fill="#888")
            self._log("二维码生成失败：%s" % exc)
            return
        n = len(m)
        size = 170
        cell = size / n
        for y, row in enumerate(m):
            x = 0
            while x < n:
                if row[x]:
                    x2 = x
                    while x2 < n and row[x2]:
                        x2 += 1
                    c.create_rectangle(x * cell, y * cell, x2 * cell, (y + 1) * cell,
                                       fill="#111111", outline="")
                    x = x2
                else:
                    x += 1

    # --------------------------------------------------------------- 事件泵
    def _pump(self):
        try:
            while True:
                self._handle(self.q.get_nowait())
        except queue.Empty:
            pass
        self.root.after(120, self._pump)

    def _handle(self, ev):
        kind = ev[0]
        if kind == "log":
            self._log(ev[1])
        elif kind == "progress":
            _, name, done, total, rate = ev
            if name != self.last_name:
                self.last_name = name
                self._log("开始：%s" % name)
            if total > 0:
                if self.indet:
                    self.bar.stop()
                    self.indet = False
                self.bar.configure(mode="determinate")
                self.bar["value"] = min(100.0, done / total * 100.0)
                self.status_var.set("%s  %s / %s" % (name, human(done), human(total)))
            else:
                if not self.indet:
                    self.bar.configure(mode="indeterminate")
                    self.bar.start(60)
                    self.indet = True
                self.status_var.set("%s  %s" % (name, human(done)))
            self.rate_var.set("%.1f MB/s" % rate if rate > 0 else "")
        elif kind == "finished":
            _, name, nbytes, secs = ev
            if self.indet:
                self.bar.stop()
                self.indet = False
            self.bar.configure(mode="determinate")
            self.bar["value"] = 100
            speed = (nbytes / 1048576.0) / max(secs, 1e-6)
            self.status_var.set("完成：%s（%s）" % (name, human(nbytes)))
            self.rate_var.set("%.1f MB/s" % speed)
            self._log("完成 %s  %s  用时 %.1fs  平均 %.1f MB/s" % (name, human(nbytes), secs, speed))
        elif kind == "failed":
            self.bar.stop()
            self.indet = False
            self.status_var.set("失败：%s" % ev[2])
            self._log("失败 %s：%s" % (ev[1], ev[2]))


def main():
    App().root.mainloop()


if __name__ == "__main__":
    main()
