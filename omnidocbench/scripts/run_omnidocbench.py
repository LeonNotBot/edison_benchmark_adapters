#!/usr/bin/env python3
"""
OmniDocBench adapter - docker-in-docker mode.

通过官方镜像 ghcr.io/zeng-weijun/omnidocbench-eval:repro-ubuntu2204 运行评测,
worker 不安装 TeX Live / ImageMagick 等重依赖。

输入: Edison external_benchmark input.json (--config)
输出: Edison external_benchmark result.json (protocol v1, 对齐 terminal-bench-2 格式)
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

OFFICIAL_IMAGE = "ghcr.io/zeng-weijun/omnidocbench-eval:repro-ubuntu2204"
ADAPTER_NAME = "omnidocbench"
DEFAULT_SUITE = "OmniDocBench/end2end"
ODB_METRIC_FILE = "end2end_quick_match_metric_result.json"

VLM_PROMPT = """You are an AI assistant specialized in converting PDF images to Markdown format. Please follow these instructions for the conversion:

1. Text Processing:
- Accurately recognize all text content in the PDF image without guessing or inferring.
- Convert the recognized text into Markdown format.
- Maintain the original document structure, including headings, paragraphs, lists, etc.

2. Mathematical Formula Processing:
- Convert all mathematical formulas to LaTeX format.
- Enclose inline formulas with \\( \\). For example: This is an inline formula \\( E = mc^2 \\)
- Enclose block formulas with \\[ \\]. For example: \\[ \\frac{-b \\pm \\sqrt{b^2 - 4ac}}{2a} \\]

3. Table Processing:
- Convert tables to HTML format.
- Wrap the entire table with <table> and </table>.

4. Figure Handling:
- Ignore figures content in the PDF image. Do not attempt to describe or convert images.

5. Output Format:
- Ensure the output Markdown document has a clear structure with appropriate line breaks between elements.
- For complex layouts, try to maintain the original document's structure and format as closely as possible.

Please strictly follow these guidelines to ensure accuracy and consistency in the conversion. Your task is to accurately convert the content of the PDF image into Markdown format without adding any extra explanations or comments."""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def expand_path(raw: str) -> Path:
    return Path(raw).expanduser().resolve()


def load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2, default=str), encoding="utf-8")


def write_progress(progress_path, current: int, total: int, stage: str = "running") -> None:
    """写增量进度快照,后端每5s轮询 _sync_task_progress 读取。写失败不影响推理。"""
    if not progress_path:
        return
    try:
        write_json(Path(progress_path), {
            "stage": stage,
            "current": current,
            "total": total,
            "completed": current,
            "running": max(0, total - current) if stage == "running" else 0,
            "pending": max(0, total - current),
            "updated_at": now_iso(),
        })
    except Exception:
        pass


def get_image_media_type(path: Path) -> str:
    suffix = path.suffix.lower()
    return {"jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png"}.get(suffix[1:], "image/jpeg")


def call_vlm_api(image_path: Path, model: dict, params: dict) -> str:
    """调用 VLM API 生成图片对应的 markdown。

    支持 Anthropic 原生协议和 OpenAI 兼容协议。
    """
    provider = (model.get("normalized_provider") or model.get("provider") or "").strip().lower()
    model_id = (model.get("model_identifier") or "").strip()

    # endpoint 优先级：model.api_endpoint > 特定 provider 的环境变量(兜底)
    endpoint = (model.get("api_endpoint") or "").strip().rstrip("/")
    if not endpoint:
        if provider == "anthropic":
            endpoint = os.getenv("ANTHROPIC_BASE_URL", "").strip().rstrip("/")
        elif provider == "openai":
            endpoint = os.getenv("OPENAI_BASE_URL", "").strip().rstrip("/")
        elif provider == "openrouter":
            endpoint = os.getenv("OPENROUTER_BASE_URL", "").strip().rstrip("/")

    # 凭证优先级：model.api_key_env 指定的环境变量 > EDISON_MODEL_API_KEY > 特定 provider 默认
    api_key_env = (model.get("api_key_env") or "").strip()
    if api_key_env:
        api_key = os.getenv(api_key_env, "")
    else:
        api_key = os.getenv("EDISON_MODEL_API_KEY", "")
        if not api_key:
            if provider == "anthropic":
                api_key = os.getenv("ANTHROPIC_API_KEY", "") or os.getenv("ANTHROPIC_AUTH_TOKEN", "")
            elif provider == "openai":
                api_key = os.getenv("OPENAI_API_KEY", "")
            elif provider == "openrouter":
                api_key = os.getenv("OPENROUTER_API_KEY", "")

    if not api_key:
        raise RuntimeError(f"VLM API key 未配置 (provider={provider})")
    if not endpoint:
        raise RuntimeError(f"VLM API endpoint 未配置")
    if not model_id:
        raise RuntimeError(f"model_identifier 未配置")

    # 读取并 base64 编码图片
    image_data = image_path.read_bytes()
    b64_data = base64.b64encode(image_data).decode("utf-8")
    media_type = get_image_media_type(image_path)

    timeout = float(params.get("vlm_timeout_seconds", 120))

    if provider == "anthropic":
        # Anthropic Messages API
        base = endpoint[:-3] if endpoint.endswith("/v1") else endpoint
        url = f"{base}/v1/messages"
        headers = {
            "content-type": "application/json",
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
        }
        payload = {
            "model": model_id,
            "max_tokens": params.get("vlm_max_tokens", 16384),  # 默认16384对齐长文档,可通过params覆盖
            "messages": [{
                "role": "user",
                "content": [
                    # content 顺序对齐官方 Qwen3-VL: 先 text 后 image
                    {"type": "text", "text": VLM_PROMPT},
                    {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": b64_data}},
                ],
            }],
        }
        # 可选参数: temperature/top_p 等,仅当 params 明确指定时添加
        if "vlm_temperature" in params:
            payload["temperature"] = params["vlm_temperature"]
        if "vlm_top_p" in params:
            payload["top_p"] = params["vlm_top_p"]
    else:
        # OpenAI 兼容 API (openai/openrouter/自定义)
        base = endpoint if endpoint.endswith("/v1") else f"{endpoint}/v1"
        url = f"{base}/chat/completions"
        headers = {
            "content-type": "application/json",
            "authorization": f"Bearer {api_key}",
        }
        payload = {
            "model": model_id,
            "messages": [{
                "role": "user",
                "content": [
                    # content 顺序对齐官方 Qwen3-VL: 先 text 后 image
                    {"type": "text", "text": VLM_PROMPT},
                    {"type": "image_url", "image_url": {"url": f"data:{media_type};base64,{b64_data}"}},
                ],
            }],
            "max_tokens": params.get("vlm_max_tokens", 16384),  # 默认16384对齐长文档,可通过params覆盖
        }
        # 可选参数: temperature/top_p 等,仅当 params 明确指定时添加
        if "vlm_temperature" in params:
            payload["temperature"] = params["vlm_temperature"]
        if "vlm_top_p" in params:
            payload["top_p"] = params["vlm_top_p"]

    # HTTP POST
    body = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        error_body = e.read().decode("utf-8", errors="replace") if e.fp else ""
        raise RuntimeError(f"VLM API 调用失败 (HTTP {e.code}): {error_body[:500]}")
    except Exception as e:
        raise RuntimeError(f"VLM API 调用失败: {e}")

    data = json.loads(raw) if raw else {}

    # 提取响应文本
    if provider == "anthropic":
        content = data.get("content", [])
        if content and isinstance(content, list) and content[0].get("type") == "text":
            return content[0].get("text", "")
        raise RuntimeError(f"Anthropic API 响应格式异常: {raw[:200]}")
    else:
        choices = data.get("choices", [])
        if choices and "message" in choices[0]:
            return choices[0]["message"].get("content", "")
        raise RuntimeError(f"OpenAI API 响应格式异常: {raw[:200]}")


def resolve_images_dir(gt_json_path: Path, params: dict) -> Path:
    """解析图片目录。优先使用 params.images_dir,否则默认为 gt_json 同目录下的 images/ 子目录。"""
    if params.get("images_dir"):
        images_dir = expand_path(str(params["images_dir"]))
        if not images_dir.exists():
            raise RuntimeError(f"params.images_dir 不存在: {images_dir}")
        return images_dir

    # 默认规则: gt_json 同目录下的 images/ (OmniDocBench 官方数据集结构)
    images_dir = gt_json_path.parent / "images"
    if not images_dir.exists():
        raise RuntimeError(f"默认 images_dir 不存在: {images_dir} (可用 params.images_dir 显式指定)")

    return images_dir


def infer_predictions(images_dir: Path, predictions_dir: Path, model: dict, params: dict,
                      only_names=None, progress_path=None) -> dict:
    """遍历图片目录,逐个调用 VLM API 生成 markdown。

    only_names: 若指定,仅推理文件名在集合内的图片(用于 limit 裁剪,保证与 GT 对齐)。
    返回推理统计信息 {total, success, failed, duration_seconds}。
    """
    image_files = sorted([
        f for f in images_dir.iterdir()
        if f.suffix.lower() in {".jpg", ".jpeg", ".png"} and f.is_file()
        and (only_names is None or f.name in only_names)
    ])

    if not image_files:
        raise RuntimeError(f"images_dir 中未找到图片文件: {images_dir}")

    total = len(image_files)
    success = 0
    failed = 0
    errors = []
    started = time.time()

    print(f"[OmniDocBench] 开始推理: {total} 张图片 → {predictions_dir}", flush=True)
    write_progress(progress_path, 0, total)

    for idx, image_path in enumerate(image_files, 1):
        md_name = image_path.stem + ".md"
        md_path = predictions_dir / md_name

        print(f"[OmniDocBench] 推理 {idx}/{total}: {image_path.name}", flush=True)

        try:
            markdown = call_vlm_api(image_path, model, params)
            md_path.write_text(markdown, encoding="utf-8")
            success += 1
        except Exception as e:
            failed += 1
            error_msg = f"{image_path.name}: {str(e)[:200]}"
            errors.append(error_msg)
            print(f"[OmniDocBench] 推理失败: {error_msg}", file=sys.stderr, flush=True)
            # 容错:继续下一张

        # 每张推理后写增量进度,后端轮询即可实时更新进度条
        write_progress(progress_path, idx, total)

    duration = time.time() - started
    print(f"[OmniDocBench] 推理完成: {success} 成功, {failed} 失败, 耗时 {duration:.1f}s", flush=True)

    return {
        "total": total,
        "success": success,
        "failed": failed,
        "duration_seconds": round(duration, 2),
        "errors": errors[:10],  # 最多记录 10 条错误
    }


def compute_scores(odb: dict) -> dict:
    """从 OmniDocBench metric_result 计算分数(对齐官方 README 公式)。

    Overall = ((1-text_edit)×100 + table_TEDS×100 + formula_CDM×100) / 3
    官方用 .page 键(按页聚合)，只算 3 维(text+table+formula)。
    reading_order 单独报告，不进 overall。

    参考: OmniDocBench/src/runtime/eval_report.py:131
          OmniDocBench/README.md:515
    """
    def _dig(d, *keys):
        # 逐层安全取值; 任一层缺失/非数值(如无表格样本时 TEDS 为空 {} 或 NaN)返回 None
        for k in keys:
            if not isinstance(d, dict) or k not in d:
                return None
            d = d[k]
        return d if isinstance(d, (int, float)) else None

    text_edit = _dig(odb, "text_block", "all", "Edit_dist", "ALL_page_avg")
    formula_page = _dig(odb, "display_formula", "page", "CDM", "ALL")
    table_page = _dig(odb, "table", "page", "TEDS", "ALL")
    reading_edit = _dig(odb, "reading_order", "all", "Edit_dist", "ALL_page_avg")

    text_block = (1.0 - text_edit) if text_edit is not None else None
    reading = (1.0 - reading_edit) if reading_edit is not None else None

    # 官方 3 维公式(百分制)。某维度无样本(如 limit 小样本无表格)时跳过该维,仅按可用维度平均。
    dims = [d for d in (text_block, table_page, formula_page) if d is not None]
    overall = sum(dims) / len(dims) if dims else 0.0

    return {
        "text_block": text_block,
        "display_formula": formula_page,
        "table": table_page,  # 无表格样本时为 None
        "reading_order": reading,  # 保留但不进 overall
        "overall": overall,
    }


PER_PAGE_FILES = {
    "text_block": "end2end_quick_match_text_block_per_page_edit.json",
    "reading_order": "end2end_quick_match_reading_order_per_page_edit.json",
    "table": "end2end_quick_match_table_per_page_edit.json",
    "display_formula": "end2end_quick_match_display_formula_per_page_edit.json",
}


def build_page_samples(odb_result: Path) -> list:
    """读 4 个 per_page_edit 文件, 按页组装逐页 samples。

    per_page 文件均存 edit_dist(越小越好), 每页分 = 1 - edit_dist。
    text_block/reading_order 覆盖全部页, table/display_formula 仅含该元素的页。
    每页 score = 该页实际存在维度的算术平均。
    """
    per_dim: dict[str, dict] = {}
    for dim, fname in PER_PAGE_FILES.items():
        fpath = odb_result / fname
        per_dim[dim] = load_json(fpath) if fpath.exists() else {}

    pages: list[str] = []
    for dim in ("text_block", "reading_order"):
        for page in per_dim[dim]:
            if page not in pages:
                pages.append(page)

    samples = []
    for idx, page in enumerate(sorted(pages)):
        dims = {}
        for dim in PER_PAGE_FILES:
            if page in per_dim[dim]:
                dims[dim] = 1.0 - float(per_dim[dim][page])
        page_score = sum(dims.values()) / len(dims) if dims else 0.0
        samples.append({
            "id": f"page_{idx:03d}",
            "name": page,
            "status": "completed",
            "score": page_score,
            "output": f"{page} score={page_score:.4f} dims={len(dims)}",
            "latency_seconds": None,
            "metrics": {"dimension_scores": dims},
            "error": None,
        })
    return samples


def build_result(*, edison_input: dict, scores: dict, odb: dict,
                 returncode: int, stdout_tail: str, stderr_tail: str,
                 duration: float | None, samples: list | None = None,
                 infer_stats: dict | None = None) -> dict:
    """组装 Edison external_benchmark result.json (protocol v1)。"""
    benchmark = edison_input.get("benchmark") or {}
    model = edison_input.get("model") or {}
    overall = scores["overall"]
    status = "completed" if returncode == 0 else "error"

    metrics = {
        "duration_seconds": duration,
        "docker_image": OFFICIAL_IMAGE,
        "docker_returncode": returncode,
        "stdout_tail": stdout_tail[-8000:],
        "stderr_tail": stderr_tail[-8000:],
        "dimension_scores": scores,
    }
    if infer_stats:
        metrics["inference"] = infer_stats

    return {
        "protocol_version": "edison.external_benchmark.v1",
        "adapter": benchmark.get("adapter") or ADAPTER_NAME,
        "benchmark": benchmark.get("suite") or DEFAULT_SUITE,
        "suite": benchmark.get("suite") or DEFAULT_SUITE,
        "agent": benchmark.get("agent") or "",
        "model": model.get("model_identifier") or "",
        "run_id": now_iso(),
        "status": status,
        "score": overall,
        "pass_rate": overall,
        "sample_count": len(samples) if samples else 1,
        "metrics": metrics,
        "samples": samples if samples else [{
            "id": "omnidocbench_end2end",
            "name": "end2end",
            "status": status,
            "score": overall,
            "output": f"OmniDocBench end2end overall={overall:.4f}",
            "latency_seconds": duration,
            "metrics": {"dimension_scores": scores, "raw_metrics": odb},
            "error": None,
        }],
        "error": stderr_tail[-4000:] if returncode != 0 else None,
    }


def error_result(edison_input: dict, message: str) -> dict:
    benchmark = edison_input.get("benchmark") or {}
    return {
        "protocol_version": "edison.external_benchmark.v1",
        "adapter": benchmark.get("adapter") or ADAPTER_NAME,
        "benchmark": benchmark.get("suite") or DEFAULT_SUITE,
        "suite": benchmark.get("suite") or DEFAULT_SUITE,
        "status": "error",
        "score": None,
        "pass_rate": None,
        "sample_count": 0,
        "samples": [],
        "error": message,
        "created_at": now_iso(),
    }


def run(config_path: Path) -> int:
    edison_input = load_json(config_path)
    paths = edison_input.get("paths") or {}
    benchmark = edison_input.get("benchmark") or {}
    params = benchmark.get("params") or {}

    result_json = expand_path(str(paths.get("result_json") or "result.json"))
    run_dir = expand_path(str(paths.get("run_dir") or result_json.parent))
    progress_path = expand_path(str(paths["progress_json"])) if paths.get("progress_json") else (run_dir / "progress.json")

    # gt.json 必须指定
    gt_raw = params.get("gt_json")
    if not gt_raw:
        write_json(result_json, error_result(edison_input, "params.gt_json 必须指定"))
        return 1

    odb_gt = expand_path(str(gt_raw))
    if not odb_gt.exists():
        write_json(result_json, error_result(edison_input, f"gt_json 不存在: {odb_gt}"))
        return 1

    # 解析图片源(用原始 GT 同目录的 images/,裁剪前解析)
    try:
        images_dir = resolve_images_dir(odb_gt, params)
    except Exception as e:
        write_json(result_json, error_result(edison_input, f"解析 images_dir 失败: {e}"))
        return 1

    # limit 裁剪: 同时裁 GT + 推理范围,保证官方 pdf_validation.py 遍历的 GT 与 pred 对齐
    limit = benchmark.get("limit")
    only_names = None
    if isinstance(limit, int) and limit > 0:
        gt_all = load_json(odb_gt)
        gt_subset = gt_all[:limit]
        only_names = {item["page_info"]["image_path"] for item in gt_subset}
        sliced_gt = run_dir / "gt_subset.json"
        write_json(sliced_gt, gt_subset)
        odb_gt = sliced_gt  # 后续 docker 挂载裁剪后的 GT
        print(f"[OmniDocBench] limit={limit} 裁剪 GT: {len(gt_all)} → {len(gt_subset)} 样本", flush=True)

    # predictions 目录:优先 params.predictions_dir,否则用 run_dir/predictions
    # 每次运行都强制调真实模型重新生成,不复用已有结果
    pred_raw = params.get("predictions_dir")
    if pred_raw:
        odb_pred = expand_path(str(pred_raw))
    else:
        odb_pred = run_dir / "predictions"

    # 清空旧 predictions,保证每次都是真实模型的新输出
    if odb_pred.exists():
        for old_md in odb_pred.glob("*.md"):
            old_md.unlink()
    odb_pred.mkdir(parents=True, exist_ok=True)

    print(f"[OmniDocBench] 强制推理生成 predictions → {odb_pred}", flush=True)
    try:
        infer_stats = infer_predictions(images_dir, odb_pred, edison_input.get("model") or {}, params,
                                         only_names=only_names, progress_path=progress_path)
    except Exception as e:
        write_json(result_json, error_result(edison_input, f"推理失败: {e}"))
        return 1

    # 检查 predictions 是否有效
    md_files = list(odb_pred.glob("*.md"))
    if not md_files:
        write_json(result_json, error_result(edison_input, f"推理未生成任何 .md 文件: {odb_pred}"))
        return 1

    odb_result = run_dir / "odb_result"
    odb_result.mkdir(parents=True, exist_ok=True)

    # docker-in-docker: 挂载 gt / predictions / result,容器内跑官方评测
    cmd = [
        "docker", "run", "--rm", "--entrypoint", "bash",
        "-v", f"{odb_gt}:/workspace/demo_data/omnidocbench_demo/OmniDocBench_demo.json:ro",
        "-v", f"{odb_pred}:/workspace/demo_data/end2end:ro",
        "-v", f"{odb_result}:/workspace/result",
        OFFICIAL_IMAGE,
        "-c", "python pdf_validation.py --config configs/end2end.yaml",
    ]
    print(f"[OmniDocBench] image={OFFICIAL_IMAGE}", flush=True)
    print(f"[OmniDocBench] cmd={' '.join(cmd)}", flush=True)

    started = datetime.now(timezone.utc)
    proc = subprocess.run(cmd, capture_output=True, text=True)
    duration = (datetime.now(timezone.utc) - started).total_seconds()
    print(proc.stdout, flush=True)
    if proc.stderr:
        print(proc.stderr, file=sys.stderr, flush=True)

    if proc.returncode != 0:
        write_json(result_json, error_result(
            edison_input, f"docker run 失败 (code={proc.returncode}): {proc.stderr[-2000:]}"))
        return proc.returncode

    metric_file = odb_result / ODB_METRIC_FILE
    if not metric_file.exists():
        write_json(result_json, error_result(
            edison_input, f"结果文件未找到: {metric_file}"))
        return 1

    odb = load_json(metric_file)
    scores = compute_scores(odb)
    page_samples = build_page_samples(odb_result)
    result = build_result(
        edison_input=edison_input, scores=scores, odb=odb,
        returncode=proc.returncode, stdout_tail=proc.stdout,
        stderr_tail=proc.stderr, duration=duration, samples=page_samples,
        infer_stats=infer_stats)
    write_json(result_json, result)
    print(f"[OmniDocBench] result -> {result_json}", flush=True)
    def _fmt(v):
        return f"{v:.4f}" if isinstance(v, (int, float)) else "N/A"
    print(f"[OmniDocBench] overall={scores['overall']:.4f} "
          f"(text={_fmt(scores['text_block'])}, formula={_fmt(scores['display_formula'])}, "
          f"table={_fmt(scores['table'])}, reading={_fmt(scores['reading_order'])})", flush=True)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True)
    args = parser.parse_args()
    return run(expand_path(args.config))


if __name__ == "__main__":
    sys.exit(main())
