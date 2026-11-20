"""下载 OCR 后端：预编译 llama-server + HunyuanOCR 官方 GGUF（免编译、省内存）。

产物落到共享数据目录（``ocr_tool.paths`` 的 ``LLAMA_BIN_DIR`` / ``MODELS_DIR``），
dev(`make run`) 与打包安装版共用同一份，避免重复下载 ~1.3GB 模型。

- llama-server：从 llama.cpp GitHub release 取预编译包解压。**不本地编译**。
  * Linux：``llama-b*-bin-ubuntu-vulkan-x64.tar.gz`` / ``-bin-ubuntu-x64.tar.gz``
  * Windows：``llama-b*-bin-win-vulkan-x64.zip`` / ``-bin-win-cpu-x64.zip``
  默认 Vulkan 版（核显加速，无 GPU 时应用会自动回退 CPU）。
  * 注意：上游把 ``releases/latest`` 改成了仅含 nightly-tag.txt 的占位 release，
    真正的二进制在以 ``bXXXXX`` 命名的 release 里。这里从 releases 列表里找
    最近一个含目标资产的 tag，兼容显式指定 LLAMA_RELEASE。
- 模型：官方 ``ggml-org/HunyuanOCR-GGUF`` 的 Q8_0 文本模型 + mmproj 视觉编码器。
  必须用官方仓库——第三方旧 GGUF 的投影层类型 ``hunyuanocr`` 在新版 llama.cpp
  已并入 HunyuanVL，会报 unknown projector type。

已存在且大小与服务端一致的文件会跳过；支持断点续传与代理下载重试。
"""

from __future__ import annotations

import concurrent.futures
import json
import os
import shutil
import sys
import tarfile
import threading
import time
import urllib.request
import zipfile
from pathlib import Path

from ocr_tool.paths import LLAMA_BIN_DIR, MODELS_DIR, ensure_data_dir

IS_WINDOWS = sys.platform == "win32"
UA = {"User-Agent": "ocr-tool-fetch"}
GGUF_REPO = os.environ.get("OCR_TOOL_HF_REPO", "ggml-org/HunyuanOCR-GGUF")
GGUF_FILES = ["HunyuanOCR-Q8_0.gguf", "mmproj-HunyuanOCR-Q8_0.gguf"]
LLAMA_RELEASE = os.environ.get("LLAMA_RELEASE", "latest").strip()
VARIANT = os.environ.get("OCR_TOOL_LLAMA_VARIANT", "vulkan").lower()

# 资产文件名后缀（按平台 + 变体）。Vulkan 默认，cpu 兜底。
if IS_WINDOWS:
    _ASSET_SUFFIX = {
        "vulkan": "-bin-win-vulkan-x64.zip",
        "cpu": "-bin-win-cpu-x64.zip",
    }
else:
    _ASSET_SUFFIX = {
        "vulkan": "-bin-ubuntu-vulkan-x64.tar.gz",
        "cpu": "-bin-ubuntu-x64.tar.gz",
    }
_SERVER_EXE = "llama-server.exe" if IS_WINDOWS else "llama-server"


def _url_candidates(url: str) -> list[str]:
    """返回同一资源的访问地址候选，直链优先、镜像/代理兜底。

    网络可达性与操作系统无关，所以这里不按平台分支；先试官方直链，
    失败再自动切到镜像/代理。候选顺序：
      - GitHub release: 直链 -> ghproxy
      - HuggingFace:    直链 -> hf-mirror
    可用环境变量覆盖：OCR_TOOL_GH_PROXY=direct 关闭代理，
    OCR_TOOL_HF_ENDPOINT 指定 HF 镜像。
    """
    from ocr_tool.win_platform import hf_base_url, resolve_github_url

    candidates = [url]
    if "github.com/" in url and "/releases/download/" in url:
        proxied = resolve_github_url(url)
        if proxied != url:
            candidates.append(proxied)
    elif "huggingface.co/" in url:
        rest = url.split("huggingface.co/", 1)[1]
        mirror = hf_base_url().rstrip("/") + "/" + rest
        if mirror != url:
            candidates.append(mirror)
    return candidates


def _api_get(url: str, *, tries: int = 6) -> object:
    """带重试的 GitHub API GET（代理对大响应偶发 IncompleteRead）。"""
    last: Exception | None = None
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=45) as r:
                return json.load(r)
        except Exception as exc:  # noqa: BLE001
            last = exc
            if i < tries - 1:
                time.sleep(1.2 * (i + 1))
    raise RuntimeError(f"GitHub API 请求失败({tries} 次): {last}")


def _remote_size(url: str) -> int:
    """探测远程大小：直链优先，失败自动试镜像；都失败返回 0（不致命）。"""
    last: Exception | None = None
    for cand in _url_candidates(url):
        try:
            req = urllib.request.Request(cand, method="HEAD", headers=UA)
            with urllib.request.urlopen(req, timeout=30) as r:
                return int(r.headers.get("Content-Length", 0))
        except Exception as exc:  # noqa: BLE001
            last = exc
            print(f"  [提示] 探测大小失败，切换下一个源: {type(exc).__name__}")
    print(f"  [提示] 无法获取远程大小（按不校验处理）: {last}")
    return 0


def _download(url: str, dst: Path, *, expected: int = 0, tries: int = 6) -> None:
    """下载文件：直链优先，失败自动切镜像；每个源内部再重试。"""
    candidates = _url_candidates(url)
    dst.parent.mkdir(parents=True, exist_ok=True)
    have = dst.stat().st_size if dst.exists() else 0
    if expected and have == expected:
        print(f"  跳过(已完整): {dst.name}")
        return
    # 各源对断点续传支持不一，优先干净重下。
    if have:
        print(f"  删除不完整文件重新下载: {dst.name} ({have} bytes)")
        dst.unlink(missing_ok=True)
    print(f"  下载 {dst.name}" + (f" ({expected / 1e6:.0f} MB)" if expected else ""))
    last: Exception | None = None
    per_source = max(1, tries // len(candidates))
    for candidate in candidates:
        print(f"    源: {candidate.split('://')[1][:70]}")
        for i in range(per_source):
            try:
                req = urllib.request.Request(candidate, headers=dict(UA))
                with urllib.request.urlopen(req, timeout=180) as r, open(dst, "wb") as o:
                    while True:
                        b = r.read(1 << 20)
                        if not b:
                            break
                        o.write(b)
                if expected and dst.stat().st_size != expected:
                    raise RuntimeError(f"下载不完整: {dst.stat().st_size}/{expected}")
                return
            except Exception as exc:  # noqa: BLE001
                last = exc
                dst.unlink(missing_ok=True)
                if i < per_source - 1:
                    print(f"    重试 {i + 1}/{per_source - 1}: {str(exc)[:80]}")
                    time.sleep(1.5 * (i + 1))
        print("    切换下一个源…")
    raise RuntimeError(f"{dst.name} 下载失败({len(candidates)} 个源): {last}")


def _download_parallel(url: str, dst: Path, *, expected: int, chunks: int = 16) -> None:
    """分段并发下载：直链优先，失败自动切镜像；每段独立重试后按序拼接。"""
    if expected <= 0:
        _download(url, dst)
        return
    candidates = _url_candidates(url)
    dst.parent.mkdir(parents=True, exist_ok=True)
    print(f"  下载 {dst.name} ({expected / 1e6:.0f} MB, {chunks} 段并发)")
    last_exc: Exception | None = None
    for candidate in candidates:
        print(f"    源: {candidate.split('://')[1][:70]}")
        try:
            _download_parallel_one(candidate, dst, expected=expected, chunks=chunks)
            return
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            shutil.rmtree(dst.parent / (dst.name + ".parts"), ignore_errors=True)
            print(f"    该源失败，切换下一个: {str(exc)[:80]}")
    raise RuntimeError(f"{dst.name} 所有源下载失败: {last_exc}")


def _download_parallel_one(url: str, dst: Path, *, expected: int, chunks: int) -> None:
    """对单个 URL 执行分段并发下载（失败抛异常，由调用方切换源）。"""
    part_size = expected // chunks
    ranges = []
    for i in range(chunks):
        start = i * part_size
        end = expected - 1 if i == chunks - 1 else (start + part_size - 1)
        ranges.append((start, end))

    parts_dir = dst.parent / (dst.name + ".parts")
    parts_dir.mkdir(parents=True, exist_ok=True)
    progress = [0] * chunks
    print_lock = threading.Lock()

    def fetch(idx: int, start: int, end: int) -> None:
        part = parts_dir / f"part-{idx:03d}"
        want = end - start + 1
        for attempt in range(8):
            have = part.stat().st_size if part.exists() else 0
            if have >= want:
                with print_lock:
                    progress[idx] = want
                return
            try:
                # 严格读取 want 字节：即使代理忽略 Range 返回整个文件，
                # 也只读本段需要的部分就停止，避免写超。
                headers = dict(UA)
                if have:
                    headers["Range"] = f"bytes={start + have}-{end}"
                else:
                    headers["Range"] = f"bytes={start}-{end}"
                req = urllib.request.Request(url, headers=headers)
                mode = "ab" if have else "wb"
                with urllib.request.urlopen(req, timeout=120) as r, open(part, mode) as o:
                    remaining = want - have
                    while remaining > 0:
                        b = r.read(min(1 << 18, remaining))
                        if not b:
                            break
                        o.write(b)
                        remaining -= len(b)
                        with print_lock:
                            progress[idx] += len(b)
                if part.stat().st_size >= want:
                    return
                # 没读满：截断到实际大小，等重试续传
            except Exception:  # noqa: BLE001
                pass
            if attempt < 7:
                time.sleep(0.8 * (attempt + 1))
        raise RuntimeError(f"分段 {idx} 下载失败 (got {part.stat().st_size if part.exists() else 0}/{want})")

    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=chunks) as ex:
            futures = [ex.submit(fetch, i, s, e) for i, (s, e) in enumerate(ranges)]
            done = 0
            last_report = 0.0
            for fut in concurrent.futures.as_completed(futures):
                fut.result()
                done += 1
                now = time.time()
                if now - last_report > 3 or done == chunks:
                    got = sum(progress)
                    print(f"    {got / 1e6:.0f}/{expected / 1e6:.0f} MB ({done}/{chunks} 段)")
                    last_report = now
        with open(dst, "wb") as out:
            for i in range(chunks):
                part = parts_dir / f"part-{i:03d}"
                want = ranges[i][1] - ranges[i][0] + 1
                with open(part, "rb") as pf:
                    left = want
                    while left > 0:
                        b = pf.read(min(1 << 18, left))
                        if not b:
                            break
                        out.write(b)
                        left -= len(b)
        if dst.stat().st_size != expected:
            raise RuntimeError(f"拼接后大小不符: {dst.stat().st_size}/{expected}")
    finally:
        shutil.rmtree(parts_dir, ignore_errors=True)


def _pick_asset(releases: list[dict], suffix: str) -> tuple[str, dict] | None:
    """在 releases 列表里找首个含目标后缀资产的 release。"""
    for rel in releases:
        for a in rel.get("assets", []):
            if a["name"].endswith(suffix) and "cudart" not in a["name"]:
                return rel["tag_name"], a
    return None


def fetch_llama_server() -> Path:
    suffix = _ASSET_SUFFIX.get(VARIANT, _ASSET_SUFFIX["vulkan"])
    api_base = "https://api.github.com/repos/ggml-org/llama.cpp/releases"

    if LLAMA_RELEASE and LLAMA_RELEASE.lower() != "latest":
        rel = _api_get(f"{api_base}/tags/{LLAMA_RELEASE}")
        releases = [rel]
    else:
        # latest 已变成占位 release；从最近若干 release 里找真正带二进制的。
        releases = _api_get(f"{api_base}?per_page=15")  # type: ignore[assignment]

    picked = _pick_asset(releases, suffix)
    if not picked:
        raise RuntimeError(
            f"未在最近的 release 里找到 {VARIANT} 预编译包({suffix})。"
            f"可用 LLAMA_RELEASE=bXXXXX 显式指定版本。"
        )
    tag, asset = picked
    print(f"[llama-server] release {tag} / {VARIANT} ({'Windows' if IS_WINDOWS else 'Linux'}): {asset['name']}")

    archive = LLAMA_BIN_DIR / asset["name"]
    expected = asset.get("size", 0)
    # 清理上次残留的不完整文件/分片目录
    archive.unlink(missing_ok=True)
    shutil.rmtree(LLAMA_BIN_DIR / (archive.name + ".parts"), ignore_errors=True)
    _download_parallel(asset["browser_download_url"], archive, expected=expected)

    if IS_WINDOWS:
        with zipfile.ZipFile(archive) as zf:
            zf.extractall(LLAMA_BIN_DIR)
    else:
        with tarfile.open(archive) as tf:
            tf.extractall(LLAMA_BIN_DIR)
    archive.unlink(missing_ok=True)

    server = next(iter(LLAMA_BIN_DIR.glob(f"**/{_SERVER_EXE}")), None)
    if not server:
        raise RuntimeError(f"解压后未找到 {_SERVER_EXE}")
    if not IS_WINDOWS:
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
    print(f"[平台] {'Windows' if IS_WINDOWS else 'Linux'} / 后端变体 {VARIANT}")
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
