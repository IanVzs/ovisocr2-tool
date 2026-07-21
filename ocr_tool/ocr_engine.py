"""HunyuanOCR 推理：本地 llama.cpp ``llama-server`` + OpenAI 兼容 HTTP 客户端。

设计：模型不再进程内加载，而是跑在本地 ``llama-server``（GGUF）里；本模块负责
拉起 / 探活 / 关闭该服务，并通过 ``/v1/chat/completions`` 发图做 OCR（流式回进度）。
无 GPU 的笔记本上，这条 llama.cpp 路径比进程内 transformers 快得多。
"""

from __future__ import annotations

import base64
import json
import logging
import os
import shutil
import signal
import subprocess
import time
from pathlib import Path
from typing import Any, Callable

ProgressCb = Callable[[float, str], None]
log = logging.getLogger("ocrtool.engine")

# HunyuanOCR 官方 task-type 提示词（固定，改词会掉点）。截图场景默认 doc_parse。
TASK_PROMPTS: dict[str, str] = {
    "doc_parse": (
        "提取文档图片中正文的所有信息用markdown格式表示，其中页眉、页脚部分忽略，"
        "表格用html格式表达，文档中公式用latex格式表示，按照阅读顺序组织进行解析。"
    ),
    "structured_parse": "提取图中的文字。",
    "formula": "识别图片中的公式，用LaTeX格式表示。",
    "table": "把图中的表格解析为HTML。",
    "chart_parse": (
        "解析图中的图表，对于流程图使用Mermaid格式表示，其他图表使用Markdown格式表示。"
    ),
}
DEFAULT_TASK = "doc_parse"


def get_prompt(task_type: str = DEFAULT_TASK) -> str:
    return TASK_PROMPTS.get(task_type, TASK_PROMPTS[DEFAULT_TASK])


def clean_repeated_substrings(
    text: str,
    *,
    min_text_len: int = 2000,
    max_period: int = 200,
    min_period: int = 1,
    min_repeat_chars: int = 80,
    min_repeat_times: int = 5,
) -> str:
    """去掉末尾截断式重复（长文档退化时常见）。"""
    n = len(text)
    if n < min_text_len:
        return text

    max_period = min(max_period, n - 1)
    for unit_len in range(min_period, max_period + 1):
        if text[n - 1] != text[n - 1 - unit_len]:
            continue
        match_len = 1
        idx = n - 2
        while idx >= unit_len and text[idx] == text[idx - unit_len]:
            match_len += 1
            idx -= 1
        total_len = match_len + unit_len
        repeat_times = total_len // unit_len
        tail_len = total_len % unit_len
        if repeat_times >= min_repeat_times and total_len >= min_repeat_chars:
            return text[: n - total_len + unit_len] + text[n - tail_len :]
    return text


def _env(name: str, default: str) -> str:
    val = os.environ.get(name)
    return val if val else default


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "").strip() or default)
    except ValueError:
        return default


def encode_image_as_data_url(image_path: str | Path, *, max_side: int) -> str:
    """读图并（超过 max_side 时）等比缩小，编码为 data URL。"""
    from PIL import Image

    img = Image.open(image_path)
    img = img.convert("RGB")
    w, h = img.size
    longest = max(w, h)
    if max_side > 0 and longest > max_side:
        scale = max_side / longest
        img = img.resize((max(1, int(w * scale)), max(1, int(h * scale))), Image.LANCZOS)

    import io

    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=92)
    b64 = base64.b64encode(buf.getvalue()).decode("ascii")
    return f"data:image/jpeg;base64,{b64}"


class HunyuanOCREngine:
    """管理本地 llama-server（HunyuanOCR GGUF），并通过 HTTP 做 OCR。"""

    def __init__(self) -> None:
        self.host = _env("OCR_TOOL_HOST", "127.0.0.1")
        self.port = _env_int("OCR_TOOL_PORT", 8080)
        # 若显式配置外部服务地址，则只连不管生命周期。
        self.external_url = os.environ.get("OCR_TOOL_SERVER_URL", "").strip()
        self.model_alias = _env("OCR_TOOL_MODEL", "HYVL")
        # 官方仓库：第三方旧 GGUF 的 hunyuanocr 投影层在新版 llama.cpp 已并入 HunyuanVL。
        self.hf_repo = _env("OCR_TOOL_HF_REPO", "ggml-org/HunyuanOCR-GGUF")
        self.model_path = os.environ.get("OCR_TOOL_MODEL_PATH", "").strip()
        self.mmproj_path = os.environ.get("OCR_TOOL_MMPROJ_PATH", "").strip()
        if not self.model_path:
            local_model, local_mmproj = self._discover_local_gguf()
            if local_model:
                self.model_path = local_model
                self.mmproj_path = self.mmproj_path or local_mmproj
        self.ctx_size = _env_int("OCR_TOOL_CTX", 8192)
        self.max_tokens = _env_int("OCR_TOOL_MAX_TOKENS", 4096)
        self.max_side = _env_int("OCR_TOOL_MAX_SIDE", 2048)
        self.task_type = _env("OCR_TOOL_TASK", DEFAULT_TASK)
        # GPU 层数默认 99（全量 offload）：Vulkan 版用核显加速；CPU 版无 GPU 后端时
        # -ngl 无副作用照样跑 CPU；Vulkan 初始化失败会自动回退 CPU（见 ensure_ready）。
        self.ngl = _env_int("OCR_TOOL_NGL", 99)
        self._ngl_fallback_done = False
        self._proc: subprocess.Popen | None = None
        self._log_fh = None

    @property
    def base_url(self) -> str:
        if self.external_url:
            return self.external_url.rstrip("/")
        return f"http://{self.host}:{self.port}"

    @property
    def manages_server(self) -> bool:
        return not self.external_url

    @property
    def loaded(self) -> bool:
        return self._is_up(timeout=1.0)

    @staticmethod
    def _models_search_dirs() -> list[Path]:
        """模型搜索目录：共享数据目录优先，源码 third_party 作兼容回退。"""
        from ocr_tool.paths import MODELS_DIR

        return [
            MODELS_DIR,
            Path(__file__).resolve().parents[1] / "third_party" / "models",
        ]

    @classmethod
    def _discover_local_gguf(cls) -> tuple[str, str]:
        """找本地 GGUF：返回 (主模型, mmproj)。

        有本地文件就用 -m/--mmproj，免依赖 `-hf`(需 libcurl) 的在线下载。
        """
        for models_dir in cls._models_search_dirs():
            if not models_dir.is_dir():
                continue
            ggufs = sorted(models_dir.glob("*.gguf"))
            mmproj = next((str(p) for p in ggufs if "mmproj" in p.name.lower()), "")
            model = next((str(p) for p in ggufs if "mmproj" not in p.name.lower()), "")
            if model:
                return model, mmproj
        return "", ""

    @staticmethod
    def find_llama_server() -> str | None:
        cand = os.environ.get("LLAMA_SERVER_BIN", "").strip()
        if cand and Path(cand).is_file():
            return cand
        found = shutil.which("llama-server")
        if found:
            return found
        from ocr_tool.paths import LLAMA_BIN_DIR

        src = Path(__file__).resolve().parents[1] / "third_party"
        # 共享数据目录(预编译)优先；再源码 third_party 预编译/源码构建产物（兼容回退）。
        for base in (LLAMA_BIN_DIR, src / "llama-bin"):
            for p in sorted(base.glob("**/llama-server")):
                if p.is_file():
                    return str(p)
        legacy = src / "llama.cpp" / "build" / "bin" / "llama-server"
        return str(legacy) if legacy.is_file() else None

    def _is_up(self, *, timeout: float = 1.0) -> bool:
        import requests

        try:
            r = requests.get(f"{self.base_url}/v1/models", timeout=timeout)
            return r.status_code == 200
        except requests.RequestException:
            return False

    def _spawn_server(self, report: ProgressCb) -> None:
        bin_path = self.find_llama_server()
        if not bin_path:
            raise RuntimeError(
                "未找到 llama-server。请先安装 llama.cpp(b8670+ 支持 HunyuanOCR 视觉)，"
                "或设置环境变量 LLAMA_SERVER_BIN 指向可执行文件。可运行 `make install-llama`。"
            )

        cmd = [bin_path, "--host", self.host, "--port", str(self.port),
               "--alias", self.model_alias,
               "--ctx-size", str(self.ctx_size),
               "--n-predict", str(self.max_tokens)]
        if self.ngl > 0:
            cmd += ["-ngl", str(self.ngl)]
        if self.model_path:
            cmd += ["-m", self.model_path]
            if self.mmproj_path:
                cmd += ["--mmproj", self.mmproj_path]
        else:
            # -hf 会从 HuggingFace 自动下载 GGUF（含同仓 mmproj），首次运行较久。
            cmd += ["-hf", self.hf_repo]

        from ocr_tool.paths import RUNTIME_DIR, ensure_runtime_dir

        ensure_runtime_dir()
        log_path = RUNTIME_DIR / "llama-server.log"
        self._log_fh = open(log_path, "a", encoding="utf-8")  # noqa: SIM115
        log.info("启动 llama-server: %s", " ".join(cmd))
        report(8.0, "启动 llama-server…")
        self._proc = subprocess.Popen(
            cmd,
            stdout=self._log_fh,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
        )

    def ensure_ready(
        self,
        *,
        on_progress: ProgressCb | None = None,
        timeout: float = 1800.0,
    ) -> None:
        """确保服务可用：已在跑则直接用；否则拉起并轮询就绪。"""

        def report(pct: float, detail: str) -> None:
            log.info("服务 %.1f%% — %s", pct, detail)
            if on_progress:
                on_progress(pct, detail)

        if self._is_up(timeout=1.5):
            report(100.0, "服务已就绪")
            return

        if not self.manages_server:
            raise RuntimeError(
                f"外部服务 {self.base_url} 未就绪（OCR_TOOL_SERVER_URL 已设置，本应用不代管）。"
            )

        if self._proc is None or self._proc.poll() is not None:
            self._spawn_server(report)

        started = time.time()
        first_download_hint = self.model_path == ""
        while time.time() - started < timeout:
            if self._proc is not None and self._proc.poll() is not None:
                code = self._proc.returncode
                # GPU 起不来（Vulkan 驱动/显存问题）→ 自动回退纯 CPU 再试一次。
                if self.ngl > 0 and not self._ngl_fallback_done:
                    self._ngl_fallback_done = True
                    log.warning("GPU 加速(-ngl %s)启动失败(code=%s)，回退纯 CPU", self.ngl, code)
                    report(9.0, "GPU 加速不可用，回退 CPU 重试…")
                    self.ngl = 0
                    self._spawn_server(report)
                    started = time.time()
                    continue
                raise RuntimeError(
                    f"llama-server 退出(code={code})，详见日志 llama-server.log。"
                    "常见原因：llama.cpp 版本过旧不支持 HunyuanOCR，或模型下载失败。"
                )
            if self._is_up(timeout=1.5):
                report(100.0, "服务已就绪")
                return
            elapsed = int(time.time() - started)
            pct = min(95.0, 10.0 + elapsed / 60.0 * 20.0)
            if first_download_hint and elapsed < 300:
                detail = f"首次运行：下载/加载模型中…已用 {elapsed}s"
            else:
                detail = f"等待服务就绪…已用 {elapsed}s"
            report(pct, detail)
            time.sleep(2.0)

        raise RuntimeError(f"llama-server 启动超时（{int(timeout)}s）。")

    def parse_image(
        self,
        image_path: str | Path,
        *,
        on_progress: ProgressCb | None = None,
    ) -> str:
        """对单张图片做 OCR，返回文本（doc_parse 默认输出 Markdown）。"""
        import requests

        def report(pct: float, detail: str, *, quiet: bool = False) -> None:
            if not quiet:
                log.info("OCR %.1f%% — %s", pct, detail)
            if on_progress:
                on_progress(pct, detail)

        if not self._is_up(timeout=1.5):
            report(5.0, "服务未就绪，正在启动…")
            self.ensure_ready(on_progress=lambda p, d: report(5 + p * 0.35, d))

        report(45.0, "预处理图像")
        image_url = encode_image_as_data_url(image_path, max_side=self.max_side)
        prompt = get_prompt(self.task_type)
        payload = {
            "model": self.model_alias,
            "messages": [
                {"role": "system", "content": ""},
                {
                    "role": "user",
                    "content": [
                        {"type": "image_url", "image_url": {"url": image_url}},
                        {"type": "text", "text": prompt},
                    ],
                },
            ],
            "max_tokens": self.max_tokens,
            "temperature": 0.0,
            "top_p": 1.0,
            "repetition_penalty": 1.08,
            "stream": True,
        }

        report(55.0, "推理中（进度会持续更新）")
        text = self._stream_completion(requests, payload, report)

        text = clean_repeated_substrings(text.strip())
        report(100.0, f"完成，约 {len(text)} 字符")
        return text

    def _stream_completion(self, requests: Any, payload: dict, report: Any) -> str:
        url = f"{self.base_url}/v1/chat/completions"
        chunks: list[str] = []
        started = time.time()
        last_ui = -999.0
        last_log = -999.0
        try:
            with requests.post(url, json=payload, stream=True, timeout=1800) as resp:
                if resp.status_code != 200:
                    resp.encoding = "utf-8"
                    body = resp.text[:300]
                    raise RuntimeError(f"服务返回 {resp.status_code}: {body}")
                # 按字节取行再显式 UTF-8 解码：SSE 无 charset 时 requests 会用 Latin-1，
                # 导致中文等多字节被拆坏成乱码。换行符不会出现在 UTF-8 多字节序列里，
                # 按 \n 切字节行是安全的。
                for rawb in resp.iter_lines(decode_unicode=False):
                    if not rawb:
                        continue
                    raw = rawb.decode("utf-8", errors="replace") if isinstance(rawb, bytes) else rawb
                    line = raw[6:] if raw.startswith("data: ") else raw
                    if line.strip() == "[DONE]":
                        break
                    try:
                        obj = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    delta = (
                        obj.get("choices", [{}])[0]
                        .get("delta", {})
                        .get("content")
                    )
                    if not delta:
                        continue
                    chunks.append(delta)
                    n = sum(len(c) for c in chunks)
                    elapsed = time.time() - started
                    by_chars = 55.0 + min(37.0, n / max(self.max_tokens * 2, 1) * 37.0)
                    by_time = 55.0 + min(35.0, elapsed / 120.0 * 35.0)
                    pct = max(by_chars, by_time)
                    detail = f"推理中…已生成 {n} 字 / 已用 {int(elapsed)}s"
                    if elapsed - last_ui >= 0.5 or n < 8:
                        report(pct, detail, quiet=True)
                        last_ui = elapsed
                    if elapsed - last_log >= 2.0:
                        log.info("OCR %.1f%% — %s", pct, detail)
                        last_log = elapsed
        except requests.RequestException as exc:
            raise RuntimeError(f"请求 llama-server 失败: {exc}") from exc

        report(93.0, "后处理结果", quiet=True)
        return "".join(chunks)

    def shutdown(self) -> None:
        """关闭本应用拉起的 llama-server（外部服务不动）。"""
        proc = self._proc
        self._proc = None
        if proc is not None and proc.poll() is None:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
            except (OSError, ProcessLookupError):
                try:
                    proc.terminate()
                except OSError:
                    pass
            try:
                proc.wait(timeout=8)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                except (OSError, ProcessLookupError):
                    pass
        if self._log_fh is not None:
            try:
                self._log_fh.close()
            except OSError:
                pass
            self._log_fh = None
