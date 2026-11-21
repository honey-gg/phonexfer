#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""生成「手机互传」App 图标（Pillow 手绘，纯色块无渐变）

4× 超采样绘制后 LANCZOS 缩回，保证边缘干净。
输出 <outdir>/AppIcon.iconset/ 并调用 iconutil 打出 AppIcon.icns。
"""

import os
import subprocess
import sys

from PIL import Image, ImageDraw

S = 4
BASE = 1024
W = BASE * S

BG = (30, 111, 184, 255)
FG = (255, 255, 255, 255)


def sc(v):
    return int(round(v * S))


def draw_master():
    img = Image.new("RGBA", (W, W), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    d.rounded_rectangle([sc(56), sc(56), sc(968), sc(968)], radius=sc(212), fill=BG)

    # 手机：左侧竖长方形
    d.rounded_rectangle([sc(170), sc(292), sc(382), sc(748)], radius=sc(46),
                        outline=FG, width=sc(26))
    d.rounded_rectangle([sc(243), sc(702), sc(309), sc(716)], radius=sc(7), fill=FG)

    # 笔记本：屏幕 + 底座梯形
    d.rounded_rectangle([sc(628), sc(316), sc(882), sc(622)], radius=sc(22),
                        outline=FG, width=sc(26))
    d.polygon([(sc(586), sc(622)), (sc(924), sc(622)),
               (sc(888), sc(694)), (sc(622), sc(694))], fill=FG)

    def arrow(y, x0, x1, lw=24, head=42):
        d.line([(sc(x0), sc(y)), (sc(x1), sc(y))], fill=FG, width=sc(lw))
        tip = sc(x1) + (sc(head) if x1 > x0 else -sc(head))
        d.polygon([(sc(x1), sc(y - head * 0.62)),
                   (tip, sc(y)),
                   (sc(x1), sc(y + head * 0.62))], fill=FG)
        r = sc(lw) // 2  # Pillow 的 line 没有圆头端点，补一个圆
        d.ellipse([sc(x0) - r, sc(y) - r, sc(x0) + r, sc(y) + r], fill=FG)

    arrow(438, 412, 556)   # → 电脑
    arrow(560, 596, 452)   # → 手机
    return img.resize((BASE, BASE), Image.LANCZOS)


ICONSET = [
    (16, "icon_16x16.png"), (32, "icon_16x16@2x.png"),
    (32, "icon_32x32.png"), (64, "icon_32x32@2x.png"),
    (128, "icon_128x128.png"), (256, "icon_128x128@2x.png"),
    (256, "icon_256x256.png"), (512, "icon_256x256@2x.png"),
    (512, "icon_512x512.png"), (1024, "icon_512x512@2x.png"),
]


def main():
    outdir = sys.argv[1] if len(sys.argv) > 1 else os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "assets")
    os.makedirs(outdir, exist_ok=True)

    master = draw_master()
    master.save(os.path.join(outdir, "icon_1024.png"))

    iconset = os.path.join(outdir, "AppIcon.iconset")
    os.makedirs(iconset, exist_ok=True)
    for px, name in ICONSET:
        master.resize((px, px), Image.LANCZOS).save(os.path.join(iconset, name))

    icns = os.path.join(outdir, "AppIcon.icns")
    subprocess.run(["iconutil", "-c", "icns", iconset, "-o", icns], check=True)
    print("图标已生成：%s" % icns)
    print("iconset 共 %d 个尺寸" % len(ICONSET))


if __name__ == "__main__":
    main()
