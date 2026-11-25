# phonexfer

手机 ↔ 电脑之间局域网双向传文件。**零依赖**——只用 Python 标准库，电脑端什么都不用装。

日常场景：手机和 Mac 同一 WiFi 下，几十 G 的文件不想折腾 USB 线和第三方 App。

## 用法

```bash
# 手机 → 电脑（默认存到 ~/Downloads/手机传入）
phonexfer recv

# 指定存放目录
phonexfer recv -d ~/Desktop

# 电脑 → 手机（把当前目录挂成网页，手机浏览器打开即可下载）
phonexfer send

# 只分享指定目录 / 单个文件
phonexfer send ~/Downloads
phonexfer send model.gguf
```

启动后终端会打印 `http://<局域网IP>:8000` 地址，手机浏览器访问即可。Mac 上打开 `…/qr` 可以扫码进入。

## 特性

- **断点续传**：8 MB 分片传输，中断后从已传位置续传，不重传已完成的字节
- **跨重启续传**：分片存在 `<保存目录>/.phonexfer-parts/`，进程被杀掉、热点掉线都不会白传
- **自动重连**：指数退避重试，热点抖动不用手动干预
- **限速选项**：页面提供 30 / 15 / 8 MB/s 三档，手机发热或降频时可用
- **图形界面**：`gui_app.py` 提供可双击的 macOS App，含二维码与实时速度显示

## 构建图形界面版

```bash
./build_app.sh                    # 默认输出到 ~/Applications/手机互传.app
./build_app.sh /path/to/xxx.app   # 指定输出路径
```

## 实测性能参考

同一热点下实测约 **49.6 MB/s（~400 Mbps）**，17.7 GB 文件。注意 WiFi 的 ping 抖动不能用来推断吞吐瓶颈——可靠依据是协商速率加实际大文件吞吐。

## 说明

绑定的地址是局域网 IP 而非 127.0.0.1，所以本机测试也要用真实 IP 访问。
首次运行时 macOS 可能弹出「允许接入网络」授权，需要点允许。

### 发送模式下不该出现的「空页面 / 权限错误」

macOS 把 `~/Downloads`、`~/Desktop` 这类目录纳入 TCC 保护。启动器是 Python 时，
系统可能不认这个「App」，于是 `os.listdir` 直接被拒（`Operation not permitted`）。

- App 启动前会先探测目标目录可读性，读不到就直接弹窗说明，不会让你把服务起起来才发现。
- 手机端若仍撞上权限问题，页面会给出中文提示页，而不是裸 JSON 500。
- 解决：系统设置 → 隐私与安全性 → 文件与文件夹，给「手机互传」勾上对应目录；或把要分享的
  内容放到不受保护的目录（例如 `~/Documents/共享`）。
