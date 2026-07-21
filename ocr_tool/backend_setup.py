"""下载 OCR 后端：预编译 llama-server + HunyuanOCR 官方 GGUF（免编译、省内存）。

产物落到共享数据目录（``ocr_tool.paths`` 的 ``LLAMA_BIN_DIR`` / ``MODELS_DIR``），
dev(`make run`) 与打包安装版共用同一份，避免重复下载 ~1.3GB 模型。

- llama-server：从 llama.cpp GitHub release 取 Linux x64 预编译包解压。**不本地编译**
  （避免多进程 g++ 打爆内存）。默认 Vulkan 版（核显加速，无 GPU 时自动跑 CPU）。
- 模型：官方 ``ggml-org/HunyuanOCR-GGUF`` 的 Q8_0 文本模型 + mmproj 视觉编码器。
  必须用官方仓库——第三方旧 GGUF 的投影层类型 ``hunyuanocr`` 在新版 llama.cpp
  已并入 HunyuanVL，会报 unknown projector type。

已存在且大小与服务端一致的文件会跳过；支持断点续传。
"""

from __future__ import annotations

import json
import os
import sys
import tarfile
import urllib.request
from pathlib import Path

from ocr_tool.paths import LLAMA_BIN_DIR, MODELS_DIR, ensure_data_dir

UA = {"User-Agent": "ocr-tool-fetch"}
GGUF_REPO = os.environ.get("OCR_TOOL_HF_REPO", "ggml-org/HunyuanOCR-GGUF")
GGUF_FILES = ["HunyuanOCR-Q8_0.gguf", "mmproj-HunyuanOCR-Q8_0.gguf"]
# 用 latest 即可（上游 b8670+ 支持 HunyuanOCR 视觉）。可用 LLAMA_RELEASE 固定版本。
LLAMA_RELEASE = os.environ.get("LLAMA_RELEASE", "latest")
# 后端变体：vulkan(默认，核显加速) 或 cpu。
VARIANT = os.environ.get("OCR_TOOL_LLAMA_VARIANT", "vulkan").lower()
_ASSET_SUFFIX = {
    "vulkan": "-bin-ubuntu-vulkan-x64.tar.gz",
    "cpu": "-bin-ubuntu-x64.tar.gz",
}


def _remote_size(url: str) -> int:
    req = urllib.request.Request(url, method="HEAD", headers=UA)
    with urllib.request.urlopen(req, timeout=60) as r:
        return int(r.headers.get("Content-Length", 0))


def _download(url: str, dst: Path, *, expected: int = 0) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    have = dst.stat().st_size if dst.exists() else 0
    if expected and have == expected:
        print(f"  跳过(已完整): {dst.name}")
        return
    headers = dict(UA)
    mode = "wb"
    if have and expected and have < expected:
        headers["Range"] = f"bytes={have}-"
        mode = "ab"
        print(f"  续传 {dst.name} 从 {have}/{expected}")
    else:
        have = 0
        print(f"  下载 {dst.name} ({expected / 1e6:.0f} MB)" if expected else f"  下载 {dst.name}")
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=120) as r, open(dst, mode) as o:
        while True:
            b = r.read(1 << 20)
            if not b:
                break
            o.write(b)
    if expected and dst.stat().st_size != expected:
        raise RuntimeError(f"{dst.name} 下载不完整: {dst.stat().st_size}/{expected}")


def fetch_llama_server() -> Path:
    if LLAMA_RELEASE == "latest":
        api = "https://api.github.com/repos/ggml-org/llama.cpp/releases/latest"
    else:
        api = f"https://api.github.com/repos/ggml-org/llama.cpp/releases/tags/{LLAMA_RELEASE}"
    req = urllib.request.Request(api, headers=UA)
    rel = json.load(urllib.request.urlopen(req, timeout=60))
    tag = rel["tag_name"]
    suffix = _ASSET_SUFFIX.get(VARIANT, _ASSET_SUFFIX["vulkan"])
    if VARIANT == "cpu":

        def ok(n: str) -> bool:
            return n.endswith(suffix) and not any(
                x in n for x in ("vulkan", "sycl", "rocm", "openvino")
            )
    else:

        def ok(n: str) -> bool:
            return n.endswith(suffix)

    asset = next((a for a in rel["assets"] if ok(a["name"])), None)
    if not asset:
        raise RuntimeError(f"未在 release 里找到 {VARIANT} 预编译包({suffix})")
    print(f"[llama-server] release {tag} / {VARIANT}: {asset['name']}")
    tarball = LLAMA_BIN_DIR / asset["name"]
    _download(asset["browser_download_url"], tarball, expected=asset.get("size", 0))
    with tarfile.open(tarball) as tf:
        tf.extractall(LLAMA_BIN_DIR)
    tarball.unlink(missing_ok=True)
    server = next(iter(LLAMA_BIN_DIR.glob("**/llama-server")), None)
    if not server:
        raise RuntimeError("解压后未找到 llama-server")
    server.chmod(0o755)
    print(f"[llama-server] 就绪: {server}")
    return server


def fetch_models() -> None:
    print(f"[model] {GGUF_REPO} (Q8_0)")
    for f in GGUF_FILES:
        base = f"https://huggingface.co/{GGUF_REPO}/resolve/main/{f}"
        _download(base, MODELS_DIR / f, expected=_remote_size(base))
    print("[model] 全部就绪")


def main() -> int:
    ensure_data_dir()
    print(f"[数据目录] {MODELS_DIR.parent}")
    try:
        fetch_llama_server()
        fetch_models()
    except Exception as exc:  # noqa: BLE001
        print(f"[错误] {exc}", file=sys.stderr)
        return 1
    print("后端就绪：llama-server + HunyuanOCR GGUF 均已本地化。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
