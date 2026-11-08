# -*- coding: utf-8 -*-
"""工具包构建入口：打包 → 产物形状校验 → sha256。

默认走**纯 Python 打包**（zipfile，离线可复现，产物只含 manifest.json / icon.png /
backend/ / frontend/ 四个根条目，与 mytool build 的包形状一致）；`--mytool` 改用官方
脚手架（`npx mybooks-tools-builder build`，需要联网拉包）。两种方式打完都跑同一道
校验：根目录必须有 manifest.json、不得混入 __pycache__/.pyc/.DS_Store/越界顶层条目、
必需文件齐全；不通过就非零退出。

用法：python scripts/build.py [--mytool] [--keep-dist]
"""
import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import zipfile

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PKG_ROOT_ENTRIES = ("manifest.json", "icon.png", "backend", "frontend")
FORBIDDEN_PARTS = ("__pycache__", ".pyc", ".DS_Store", ".porting")
REQUIRED_FILES = (
    "manifest.json", "icon.png",
    "backend/__init__.py", "backend/tool.py", "backend/zlib_client.py",
    "backend/accounts.py",
    "frontend/index.html", "frontend/app.js",
    "frontend/lib/theme.css", "frontend/lib/i18n.js",
    "frontend/locales/zh.json", "frontend/locales/en.json",
    "frontend/locales/zh-TW.json", "frontend/locales/manifest.json",
)
PKG_DIRS = ("backend", "frontend")


def clean_bytecode():
    removed = 0
    for dirpath, dirnames, filenames in os.walk(REPO_ROOT):
        if ".git" in dirpath or "dist" in dirpath:
            continue
        for name in list(dirnames):
            if name == "__pycache__":
                shutil.rmtree(os.path.join(dirpath, name), ignore_errors=True)
                dirnames.remove(name)
                removed += 1
        for name in filenames:
            if name.endswith((".pyc", ".pyo")):
                os.remove(os.path.join(dirpath, name))
                removed += 1
    print("清理字节码：移除 %d 项" % removed)


def read_manifest():
    with open(os.path.join(REPO_ROOT, "manifest.json"), encoding="utf-8") as f:
        return json.load(f)


def pack_with_zipfile(dest_zip):
    """纯 Python 打包：只收四个根条目，文本以仓库内原始字节写入（.gitattributes
    保证 checkout 是 LF，与脚手架产物一致）。返回条目数。"""
    count = 0
    with zipfile.ZipFile(dest_zip, "w", zipfile.ZIP_DEFLATED) as zf:
        for root_name in ("manifest.json", "icon.png"):
            zf.write(os.path.join(REPO_ROOT, root_name), arcname=root_name)
            count += 1
        for pkg_dir in PKG_DIRS:
            base = os.path.join(REPO_ROOT, pkg_dir)
            for dirpath, dirnames, filenames in os.walk(base):
                dirnames[:] = [d for d in dirnames if d != "__pycache__"]
                for name in sorted(filenames):
                    if name.endswith((".pyc", ".pyo")):
                        continue
                    full = os.path.join(dirpath, name)
                    arc = os.path.relpath(full, REPO_ROOT).replace(os.sep, "/")
                    zf.write(full, arcname=arc)
                    count += 1
    return count


def pack_with_mytool():
    cmd = ["npx", "--yes", "mybooks-tools-builder@latest", "build"]
    print("运行：%s" % " ".join(cmd))
    result = subprocess.run(cmd, cwd=REPO_ROOT, shell=(os.name == "nt"))
    if result.returncode != 0:
        sys.exit(result.returncode)


def verify_zip():
    dist_dir = os.path.join(REPO_ROOT, "dist")
    zips = [f for f in os.listdir(dist_dir) if f.endswith(".zip")]
    if len(zips) != 1:
        sys.exit("dist/ 下应恰好有一个 zip，实际：%s" % zips)
    zip_path = os.path.join(dist_dir, zips[0])

    with zipfile.ZipFile(zip_path) as zf:
        names = zf.namelist()

    errors = []
    if "manifest.json" not in names:
        errors.append("zip 根目录没有 manifest.json（宿主只读归档根）")
    for entry in names:
        head = entry.split("/")[0]
        if head not in PKG_ROOT_ENTRIES:
            errors.append("出现不该在包里的顶层条目：%s" % entry)
        if any(part in entry for part in FORBIDDEN_PARTS):
            errors.append("出现应排除的文件：%s" % entry)
    for required in REQUIRED_FILES:
        if required not in names:
            errors.append("缺少必需文件：%s" % required)

    if errors:
        print("✗ 产物校验失败：")
        for line in errors:
            print("  - %s" % line)
        sys.exit(1)

    with open(zip_path, "rb") as f:
        digest = hashlib.sha256(f.read()).hexdigest()
    print("✔ 产物校验通过：%s（%d 个条目）" % (os.path.relpath(zip_path, REPO_ROOT), len(names)))
    print("  sha256: %s" % digest)
    return zip_path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--mytool", action="store_true",
                        help="用官方脚手架（npx mybooks-tools-builder）打包；默认纯 Python 打包")
    parser.add_argument("--keep-dist", action="store_true", help="保留上一次 dist 产物")
    args = parser.parse_args()

    manifest = read_manifest()
    if not args.keep_dist:
        shutil.rmtree(os.path.join(REPO_ROOT, "dist"), ignore_errors=True)
    os.makedirs(os.path.join(REPO_ROOT, "dist"), exist_ok=True)
    clean_bytecode()

    if args.mytool:
        pack_with_mytool()
    else:
        dest = os.path.join(REPO_ROOT, "dist",
                            "%s-%s.zip" % (manifest["tool_id"], manifest["revision"]))
        count = pack_with_zipfile(dest)
        print("纯 Python 打包：%s（%d 个条目）" % (os.path.relpath(dest, REPO_ROOT), count))

    verify_zip()


if __name__ == "__main__":
    main()
