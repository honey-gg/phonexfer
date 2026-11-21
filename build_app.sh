#!/bin/bash
# 把 gui_app.py 打成可双击的 macOS .app
# 不用 py2app，直接手搓 bundle（轻、无需拉依赖）
set -euo pipefail

SRC="$(cd "$(dirname "$0")" && pwd)"
APP="${1:-$HOME/Applications/手机互传.app}"
PYV="/Users/lkgs/.workbuddy/binaries/python/versions/3.13.12"
VENV="/Users/lkgs/.workbuddy/binaries/python/envs/default"
PY="$VENV/bin/python"

echo "==> 源目录: $SRC"
echo "==> 目标:   $APP"

echo "==> 1/5 重建 bundle 骨架"
rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources/app"

echo "==> 2/5 拷入程序"
cp "$SRC/phonexfer.py" "$SRC/gui_app.py" "$APP/Contents/Resources/app/"

echo "==> 3/5 写 Info.plist"
cat > "$APP/Contents/Info.plist" <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleName</key><string>手机互传</string>
  <key>CFBundleDisplayName</key><string>手机互传</string>
  <key>CFBundleExecutable</key><string>launcher.sh</string>
  <key>CFBundleIdentifier</key><string>local.phonexfer.gui</string>
  <key>CFBundleInfoDictionaryVersion</key><string>6.0</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleShortVersionString</key><string>1.0</string>
  <key>CFBundleVersion</key><string>1</string>
  <key>LSMinimumSystemVersion</key><string>11.0</string>
  <key>NSHighResolutionCapable</key><true/>
</dict>
</plist>
PLIST

echo "==> 4/5 写启动器（含 TCL_LIBRARY 修补）"
cat > "$APP/Contents/MacOS/launcher.sh" <<SH
#!/bin/bash
# 由 build_app.sh 生成，请勿手改；改逻辑请改 gui_app.py 后重新构建
APPROOT="\$(cd "\$(dirname "\$0")/.." && pwd)"
APPDIR="\$APPROOT/Resources/app"

PY="$PY"
PYV="$PYV"

# 托管 Python 的 _tkinter 把默认 Tcl 路径指向了不存在的 /tools/deps，
# 从 Finder 双击启动时环境里没有 TCL_LIBRARY，会报 "Cannot find a usable init.tcl"。
if [ -d "\$PYV/lib/tcl9.0" ]; then
  export TCL_LIBRARY="\$PYV/lib/tcl9.0"
fi
if [ -d "\$PYV/lib/tk9.0" ]; then
  export TK_LIBRARY="\$PYV/lib/tk9.0"
fi

cd "\$APPDIR"
exec "\$PY" -B "\$APPDIR/gui_app.py"
SH
chmod +x "$APP/Contents/MacOS/launcher.sh"

echo "==> 5/5 注入图标"
if [ -f "$SRC/assets/AppIcon.icns" ]; then
  mkdir -p "$APP/Contents/Resources"
  cp "$SRC/assets/AppIcon.icns" "$APP/Contents/Resources/"
  PLIST="$APP/Contents/Info.plist"
  /usr/libexec/PlistBuddy -c "Delete :CFBundleIconFile" "$PLIST" 2>/dev/null || true
  /usr/libexec/PlistBuddy -c "Add :CFBundleIconFile string AppIcon.icns" "$PLIST"
  plutil -lint "$PLIST" >/dev/null
else
  echo "    没有 assets/AppIcon.icns，先跑 make_icon.py"
fi

# 脚本生成的 .app 会被 Gatekeeper 打上隔离属性
xattr -dr com.apple.quarantine "$APP" 2>/dev/null || true

# 刷新图标缓存，否则 Finder/Dock 还显示旧图标
/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister -f "$APP" 2>/dev/null || true

echo "==> 完成：$APP"
