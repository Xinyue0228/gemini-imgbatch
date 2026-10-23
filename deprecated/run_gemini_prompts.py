#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
run_gemini_prompts.py — 用 Gemini 账号（免 API key）批量生图，自动续跑

和 codex-imgbatch（batch_imagegen2.py）同思路：不申请 API key，直接复用
Gemini CLI（gemini 命令）的 OAuth 登录，走你 Gemini Pro 订阅的额度。

每个 prompt 等价于执行：
    gemini --yolo -m <图像模型> -p "Generate an image: ... Save as xxx.png"

模型用内置生图能力（Nano Banana）把 PNG 直接写进工作目录，脚本再把图归到
output 下，并按 sample_id 命名、校验 PNG 完整性。

────────────────────────────────────────────────────────────
输入格式（与 run_my_prompts.py 完全一致）
────────────────────────────────────────────────────────────
· CSV（.csv）：至少一列叫「prompt」。若有 sample_id / aspect_ratio 列会
  自动当作输出文件名 / 宽高比。例如 document_prompts_unified.csv。
· TXT（.txt）：每行一条 prompt，# 开头和空行忽略。
· JSONL（.jsonl）：{id, prompt, aspect} 一行一条。

────────────────────────────────────────────────────────────
用法
────────────────────────────────────────────────────────────
  python run_gemini_prompts.py --input 我的prompt.csv --n 50
  python run_gemini_prompts.py --input 我的prompt.txt --n 20 --default-aspect 3:4
  python run_gemini_prompts.py --input 我的prompt.csv --check     # 只体检，不花额度
  python run_gemini_prompts.py --input 我的prompt.csv --dry-run   # 只预览，不花额度
  python run_gemini_prompts.py --input 我的prompt.csv --reset     # 从头重跑
  python run_gemini_prompts.py --input 我的prompt.csv --start 100 --n 20
  python run_gemini_prompts.py --input 我的prompt.csv --fast      # 用 Flash 模型（快）
  python run_gemini_prompts.py --input 我的prompt.csv --proxy http://127.0.0.1:7890

进度说明：进度存在 output/_progress/<文件名>.progress.json，跟 prompt 文件
绑定，换文件自动从 0 开始，断电/中断也不丢。
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent

# 与 run_my_prompts.py 一致：只有这些比例会被接受
VALID_ASPECTS = {"1:1", "3:2", "2:3", "16:9", "9:16", "4:3", "3:4"}
DEFAULT_ASPECT = "1:1"

# 宽高比 -> 自然语言提示（Nano Banana 通过描述理解比例）
ASPECT_HINT = {
    "1:1":  "square 1:1",
    "3:2":  "landscape 3:2",
    "2:3":  "portrait 2:3",
    "16:9": "widescreen landscape 16:9",
    "9:16": "vertical portrait 9:16",
    "4:3":  "landscape 4:3",
    "3:4":  "portrait 3:4",
}

# 当前图像模型（2026 年 GA，取自 Google 官方文档 ai.google.dev/gemini-api/docs/image-generation）
#   Nano Banana Pro     = gemini-3-pro-image          （非 Flash，质量最高、文字渲染最好）
#   Nano Banana 2.1     = gemini-nano-banana-2.1      （最新主力，Flash 级速度 + 更好文字，推荐新项目）
#   Nano Banana 2       = gemini-3.1-flash-image      （上一代主力，即 Flash 那一路）
#   Nano Banana 2 Lite  = gemini-3.1-flash-lite-image （最快最便宜）
#   Nano Banana（旧）   = gemini-2.5-flash-image      （旧版先驱，建议迁移）
# ⚠️ 带 -preview 后缀的（如 gemini-3-pro-image-preview）已停用，勿再用。
DEFAULT_MODELS = ["gemini-3-pro-image", "gemini-nano-banana-2.1"]
FAST_MODELS = ["gemini-nano-banana-2.1", "gemini-3.1-flash-image"]

SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
WIN_RESERVED = {"con", "prn", "aux", "nul", *(f"com{i}" for i in range(1, 10)),
                *(f"lpt{i}" for i in range(1, 10))}


def iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).astimezone().isoformat(timespec="seconds")


def _read_text_auto(path: Path) -> str:
    """按编码自动检测读取：UTF-8（含 BOM）优先，其次 GBK/GB18030，最后兜底。"""
    data = path.read_bytes()
    for enc in ("utf-8-sig", "utf-8", "gb18030"):
        try:
            return data.decode(enc)
        except (UnicodeDecodeError, ValueError):
            continue
    return data.decode("utf-8", errors="replace")


def _fix_console() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except Exception:
            pass


# ---------------------------------------------------------------- 读取 prompt 文件

def _pick_col(headers: list[str], candidates: tuple[str, ...]) -> str | None:
    norm = {h.strip().lower(): h for h in headers if h and h.strip()}
    for cand in candidates:
        if cand.lower() in norm:
            return norm[cand.lower()]
    for cand in candidates:
        for k, orig in norm.items():
            if cand.lower() in k or k in cand.lower():
                return orig
    return None


def _load_csv(path: Path) -> list[dict]:
    rows: list[dict] = []
    seen: set[str] = set()
    reader = csv.DictReader(io.StringIO(_read_text_auto(path)))
    headers = reader.fieldnames or []
    id_col = _pick_col(headers, ("sample_id", "id", "编号", "image_id"))
    prompt_col = _pick_col(headers, ("prompt", "prompt_text", "text", "描述", "内容"))
    aspect_col = _pick_col(headers, ("aspect_ratio", "aspect", "ratio", "比例", "宽高比"))
    if not prompt_col:
        raise SystemExit(
            f"[错误] CSV 里找不到「prompt」这一列。\n"
            f"  读到的表头：{headers}\n"
            f"  请确认有一列叫 prompt（或 prompt_text / text / 描述 / 内容）。")
    for i, row in enumerate(reader, 1):
        prompt = (row.get(prompt_col) or "").strip()
        if not prompt:
            continue
        rid = (row.get(id_col) or "").strip() if id_col else ""
        if not rid or not SAFE_ID.match(rid) or rid.lower().split(".")[0] in WIN_RESERVED:
            rid = f"row-{i:06d}"
        if rid in seen:
            rid = f"{rid}-{i}"
        seen.add(rid)
        aspect = (row.get(aspect_col) or "").strip() if aspect_col else ""
        if aspect not in VALID_ASPECTS:
            aspect = DEFAULT_ASPECT
        rows.append({"id": rid, "prompt": prompt, "aspect": aspect})
    return rows


def _load_txt(path: Path) -> list[dict]:
    rows: list[dict] = []
    for i, line in enumerate(_read_text_auto(path).splitlines(), 1):
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        rows.append({"id": f"p-{i:06d}", "prompt": s, "aspect": DEFAULT_ASPECT})
    return rows


def _load_jsonl(path: Path) -> list[dict]:
    rows: list[dict] = []
    for i, line in enumerate(_read_text_auto(path).splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError as e:
            raise SystemExit(f"[错误] {path.name} 第 {i} 行不是合法 JSON：{e}")
        prompt = str(obj.get("prompt", "")).strip()
        if not prompt:
            continue
        aspect = str(obj.get("aspect", DEFAULT_ASPECT))
        if aspect not in VALID_ASPECTS:
            aspect = DEFAULT_ASPECT
        rows.append({"id": str(obj.get("id") or f"row-{i:06d}"),
                     "prompt": prompt, "aspect": aspect})
    return rows


def load_prompts(path: Path, default_aspect: str) -> list[dict]:
    global DEFAULT_ASPECT
    if default_aspect not in VALID_ASPECTS:
        raise SystemExit(f"[错误] --default-aspect 只支持 {sorted(VALID_ASPECTS)}")
    DEFAULT_ASPECT = default_aspect

    ext = path.suffix.lower()
    if ext == ".csv":
        rows = _load_csv(path)
    elif ext in (".txt", ".text"):
        rows = _load_txt(path)
    elif ext in (".jsonl", ".ndjson"):
        rows = _load_jsonl(path)
    else:
        head = _read_text_auto(path)[:1]
        rows = _load_jsonl(path) if head == "{" else _load_txt(path)

    if not rows:
        raise SystemExit(f"[错误] {path.name} 里没有读到任何 prompt（空文件或格式不识别）。")
    return rows


# ---------------------------------------------------------------- 进度（续跑）

def _file_sha(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()[:16]


def load_progress(path: Path, prog_file: Path, total: int) -> int:
    sha = _file_sha(path)
    if prog_file.exists():
        try:
            d = json.loads(prog_file.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            d = {}
        if d.get("input_sha") == sha and d.get("total") == total:
            idx = int(d.get("next_index", 0))
            return max(0, min(idx, total))
        print(f"  [提示] 检测到 prompt 文件已变化（或进度文件损坏），进度从 0 重新开始。",
              file=sys.stderr)
    return 0


def save_progress(prog_file: Path, input_path: Path, total: int, next_index: int) -> None:
    d = {
        "input": str(input_path.resolve()),
        "input_sha": _file_sha(input_path),
        "total": total,
        "next_index": next_index,
        "updated_at": iso(time.time()),
    }
    prog_file.parent.mkdir(parents=True, exist_ok=True)
    tmp = prog_file.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, prog_file)


# ---------------------------------------------------------------- PNG 完整性

def png_ok(path: Path) -> bool:
    """PNG 必须带 PNG 魔数开头 + IEND 结尾才算完整。"""
    try:
        if path.stat().st_size < 100:
            return False
        with open(path, "rb") as f:
            head = f.read(8)
            if head != b"\x89PNG\r\n\x1a\n":
                return False
            f.seek(-12, os.SEEK_END)
            tail = f.read(12)
        return len(tail) == 12 and tail[4:8] == b"IEND"
    except OSError:
        return False


# ---------------------------------------------------------------- Gemini CLI

def find_gemini(explicit: str = "") -> str:
    """定位 gemini 可执行文件。Windows 优先 gemini.cmd（npm 全局装的那份）。"""
    if explicit:
        p = shutil.which(explicit) or (explicit if Path(explicit).exists() else "")
        if not p:
            raise SystemExit(f"[错误] 找不到 gemini：{explicit}")
        return p
    if os.name == "nt":
        for name in ("gemini.cmd", "gemini"):
            p = shutil.which(name)
            if p:
                return p
    else:
        p = shutil.which("gemini")
        if p:
            return p
    raise SystemExit(
        "[错误] 找不到 gemini 命令。\n"
        "  请先安装 Gemini CLI：  npm install -g @google/gemini-cli\n"
        "  或用 --gemini 显式指定路径。")


def gemini_login_state() -> tuple[str, bool]:
    """读 ~/.gemini/google_accounts.json 判断是否已用 Google 账号登录。"""
    gac = Path.home() / ".gemini" / "google_accounts.json"
    if not gac.exists():
        return "未检测到登录（~/.gemini/google_accounts.json 不存在）", False
    try:
        d = json.loads(gac.read_text(encoding="utf-8"))
    except Exception:
        return "登录状态文件损坏，请重新登录", False
    active = d.get("active")
    if active:
        return f"已登录：{active}", True
    return "尚未登录（没有 active 账号）", False


def build_image_prompt(task: dict) -> str:
    pid = task["id"]
    hint = ASPECT_HINT.get(task.get("aspect", DEFAULT_ASPECT), task.get("aspect", DEFAULT_ASPECT))
    return (
        "Generate a single image. Aspect ratio: {hint}.\n\n"
        "IMAGE DESCRIPTION:\n{desc}\n\n"
        "After generating, save the image as a PNG file named exactly \"{pid}.png\" "
        "in the current working directory. Do not create any other files."
    ).format(hint=hint, desc=task["prompt"], pid=pid)


def kill_tree(proc: subprocess.Popen) -> None:
    """超时后杀进程树，避免留下孤儿 gemini 进程继续跑、继续烧额度。"""
    if proc is None or proc.poll() is not None:
        return
    if os.name == "nt":
        try:
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                           capture_output=True)
        except Exception:
            pass
    try:
        proc.kill()
    except Exception:
        pass


def run_guarded(cmd: list[str], timeout: int, env=None, cwd: Path | None = None):
    """Popen + communicate + kill_tree + 二次 communicate。

    为什么不用 subprocess.run(timeout=...)：Windows 上超时后只杀直接子进程，
    孤儿孙进程仍持有管道，run() 的读线程永不结束，timeout 形同虚设。
    """
    t0 = time.time()
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            env=env, cwd=str(cwd) if cwd else None)
    try:
        out, err = proc.communicate(timeout=timeout)
        rc = proc.returncode
        timed_out = False
    except subprocess.TimeoutExpired:
        kill_tree(proc)
        out, err = proc.communicate()
        rc = -1
        timed_out = True
    out_s = out.decode("utf-8", errors="replace") if out else ""
    err_s = err.decode("utf-8", errors="replace") if err else ""
    return rc, out_s, err_s, timed_out, round(time.time() - t0, 1)


def run_gemini_once(task: dict, args, workdir: Path, model: str):
    env = dict(os.environ)
    # 认证方式：--api-key 显式给 key > 继承环境变量 GEMINI_API_KEY > 走 OAuth
    if args.api_key:
        env["GEMINI_API_KEY"] = args.api_key
    else:
        # 走 OAuth 订阅：删掉可能存在的 API key 环境变量，防止误走按量计费
        for k in ("GEMINI_API_KEY", "GOOGLE_API_KEY", "GOOGLE_GENAI_API_KEY",
                  "NANOBANANA_GEMINI_API_KEY", "NANOBANANA_GOOGLE_API_KEY"):
            env.pop(k, None)
    # 信任当前工作目录：否则 --yolo 会被覆盖回 default，无头模式直接卡死
    env["GEMINI_CLI_TRUST_WORKSPACE"] = "true"
    # Workspace/Cloud 账号：官方要求设 GOOGLE_CLOUD_PROJECT 才能通过认证
    if getattr(args, "project", ""):
        env["GOOGLE_CLOUD_PROJECT"] = env["GOOGLE_CLOUD_PROJECT_ID"] = args.project
    # 代理：--proxy 显式 > none 直连 > 继承终端已有代理
    proxy = (args.proxy or "").strip()
    if proxy.lower() == "none":
        for k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy",
                  "ALL_PROXY", "all_proxy"):
            env.pop(k, None)
    elif proxy:
        env["HTTPS_PROXY"] = env["HTTP_PROXY"] = proxy
        env["https_proxy"] = env["http_proxy"] = proxy

    cmd = [args.gemini, "--skip-trust", "--yolo", "-m", model,
           "-p", build_image_prompt(task)]
    rc, out, err, timed_out, elapsed = run_guarded(cmd, args.timeout, env=env, cwd=workdir)
    # 完整输出落盘，便于排查 auth / 模型 / 代理等问题
    try:
        (workdir / "gemini.log").write_text(
            f"=== command ===\n{' '.join(cmd)}\n"
            f"=== rc={rc} timed_out={timed_out} elapsed={elapsed}s ===\n"
            f"--- stdout ---\n{out}\n--- stderr ---\n{err}\n",
            encoding="utf-8")
    except OSError:
        pass
    return rc, out, err, timed_out, elapsed


def _last_error_line(out: str, err: str, n: int = 240) -> str:
    """从输出里挑最后一行最像报错的（跳过 JS 堆栈行），避免被无意义提示淹没。"""
    lines = [l.strip() for l in ((err or "") + "\n" + (out or "")).splitlines()
             if l.strip()]
    pat = re.compile(r"error|exhausted|quota|429|too many requests|denied|forbidden|"
                     r"permission|invalid|unauthorized|untrusted|"
                     r"not (eligible|available|authorized|logged)|requires",
                     re.IGNORECASE)
    for l in reversed(lines):
        if l.startswith("at "):          # 跳过 "at async xxx.js:12" 这类堆栈行
            continue
        if pat.search(l):
            return l[:n]
    return (lines[-1][:n] if lines else "(无输出)")


def find_output_png(workdir: Path) -> Path | None:
    """在本次工作目录里找真正由模型写出的 PNG（递归，取最新、且完整的那张）。"""
    cands = [p for p in workdir.rglob("*.png") if png_ok(p)]
    if not cands:
        return None
    return max(cands, key=lambda p: p.stat().st_mtime)


def run_one(task: dict, args, outdir: Path, models: list[str],
            lock: threading.Lock, counter: dict) -> dict:
    pid = task["id"]
    final = outdir / f"{pid}.png"
    if png_ok(final):
        with lock:
            counter["done"] += 1
            counter["cached"] += 1
            print(f"[cached] {pid}")
        return {"id": pid, "status": "cached", "model": "", "file": str(final),
                "attempts": 0, "error": ""}

    workdir = outdir / "_work" / pid
    workdir.mkdir(parents=True, exist_ok=True)

    model_used = ""
    last_err = ""
    attempts = 0
    for attempt in range(1, args.retries + 2):
        attempts = attempt
        for model in models:
            # 每轮先清旧 PNG，避免捡到上一轮的产物
            for p in workdir.rglob("*.png"):
                try:
                    p.unlink()
                except OSError:
                    pass
            rc, out, err, timed_out, elapsed = run_gemini_once(task, args, workdir, model)
            if timed_out:
                last_err = f"超时>{args.timeout}s (model={model})"
                continue
            if rc == 0:
                src = find_output_png(workdir)
                if src is not None:
                    shutil.copyfile(str(src), str(final))
                    if png_ok(final):
                        model_used = model
                        break
                last_err = f"无有效 PNG 产出 (model={model}, rc={rc})"
                continue
            last_err = f"rc={rc} (model={model}): {_last_error_line(out, err)}"
        if model_used:
            break

    status = "ok" if model_used else "fail"
    with lock:
        counter["done"] += 1
        if status == "ok":
            counter["ok"] += 1
            print(f"[OK ] {pid}  {model_used}")
        else:
            counter["fail"] += 1
            print(f"[FAIL] {pid}  {last_err}")
            print(f"       完整日志: {workdir / 'gemini.log'}")
    return {"id": pid, "status": status, "model": model_used,
            "file": str(final) if model_used else "", "attempts": attempts,
            "error": "" if model_used else last_err}


# ---------------------------------------------------------------- 交互询问

def _ask_n(default: int) -> int:
    try:
        raw = input(f"本次生成几张？[默认 {default}]：").strip()
    except (EOFError, KeyboardInterrupt):
        print()
        return default
    if not raw:
        return default
    if not raw.isdigit() or int(raw) <= 0:
        print("  [提示] 请输入正整数，本次使用默认值。", file=sys.stderr)
        return default
    return int(raw)


def _clean_dirname(name: str) -> str:
    return re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_", name).strip().strip(".")


def _ask_folder(default: str) -> str:
    if not sys.stdin.isatty():
        return default
    try:
        raw = input(f"是否新建一个文件夹存放这批图片？(y/n，默认 n，直接放「{default}」)：").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        return default
    if raw in ("y", "yes", "是"):
        while True:
            try:
                name = input("请输入文件夹名：").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                return default
            name = _clean_dirname(name)
            if name:
                return name
            print("  文件夹名不能为空，请重新输入。")
    return default


# ---------------------------------------------------------------- main

def _precheck(args) -> None:
    print("──────── 环境体检 ────────")
    gemini = find_gemini(args.gemini)
    print(f"gemini     : {gemini}")
    rc, out, err, timed_out, _ = run_guarded([gemini, "--version"], 30)
    if timed_out or rc != 0:
        print(f"  [警告] gemini --version 异常（rc={rc}），请确认 Gemini CLI 安装正确。")
    else:
        print(f"版本       : {(out or err).strip()}")
    state, logged = gemini_login_state()
    print(f"登录状态   : {state}")
    if not logged:
        print("\n  还没登录。请先手动执行一次下面的命令完成 Google 账号登录：")
        print("\n      gemini\n")
        print("  选「Sign in with Google」，浏览器弹出后登录你的 Gemini Pro 账号，")
        print("  完成后关掉浏览器/终端，再回来跑本脚本即可。")
    print("──────────────────────────")


def main() -> int:
    _fix_console()
    ap = argparse.ArgumentParser(
        description="用 Gemini 账号（免 API key）批量生图，自动续跑")
    ap.add_argument("--input", required=True, help="prompt 文件：.csv / .txt / .jsonl")
    ap.add_argument("--output-dir", default="", help="输出根目录。默认本脚本同级目录下 output/")
    ap.add_argument("--folder", default="", help="本次输出子文件夹名（跳过询问）")
    ap.add_argument("--n", type=int, default=0, help="本次生成张数；0 或不传 = 交互询问")
    ap.add_argument("--start", type=int, default=None, help="手动指定起始序号（0 起）")
    ap.add_argument("--reset", action="store_true", help="从头开始")
    ap.add_argument("--default-aspect", default="1:1",
                    help="TXT/缺省时的宽高比，可选 %s" % ",".join(sorted(VALID_ASPECTS)))
    ap.add_argument("--list", action="store_true", help="只列出文件概况和示例，不生成")
    ap.add_argument("--dry-run", action="store_true", help="只预览，不花额度、不动进度")
    ap.add_argument("--check", action="store_true", help="只做环境体检，不生成")
    ap.add_argument("--force", action="store_true", help="未登录也继续尝试（默认未登录直接拦截）")
    ap.add_argument("--gemini", default="", help="gemini 可执行文件路径（默认自动找）")
    ap.add_argument("--model", default="",
                    help="图像模型，逗号分隔为回退链。默认 %s" % ",".join(DEFAULT_MODELS))
    ap.add_argument("--fast", action="store_true", help="用 Nano Banana 2.1 / Flash 模型（快）")
    ap.add_argument("--concurrency", type=int, default=2)
    ap.add_argument("--timeout", type=int, default=600)
    ap.add_argument("--retries", type=int, default=2)
    ap.add_argument("--proxy", default="",
                    help="给 gemini 用的 HTTP 代理（如 http://127.0.0.1:7890）；"
                         "填 none 直连，不填继承终端代理")
    ap.add_argument("--project", default="",
                    help="Google Cloud 项目 ID：Workspace/Cloud 账号可能需设 "
                         "GOOGLE_CLOUD_PROJECT 才能通过认证")
    ap.add_argument("--api-key", default="",
                    help="用 Gemini API key 生图（AI Studio 获取，AIza 开头），"
                         "替代 OAuth 登录。不填则走 OAuth")
    args = ap.parse_args()

    args.gemini = find_gemini(args.gemini)

    if args.check:
        _precheck(args)
        return 0

    inp = Path(args.input)
    if not inp.exists():
        raise SystemExit(f"[错误] 找不到 prompt 文件：{inp}")

    prompts = load_prompts(inp, args.default_aspect)
    total = len(prompts)

    root = Path(args.output_dir).resolve() if args.output_dir else (HERE / "output")
    prog_file = root / "_progress" / f"{inp.stem}.progress.json"

    if args.start is not None:
        next_index = max(0, min(args.start, total))
    elif args.reset:
        next_index = 0
    else:
        next_index = load_progress(inp, prog_file, total)

    done = next_index
    remaining = total - next_index

    print(f"prompt 文件 : {inp}")
    print(f"共 {total} 条  已生成 {done} 条  剩余 {remaining} 条  下次从第 {next_index + 1} 条开始")
    print(f"输出根目录  : {root}")

    if args.list:
        print("\n示例（前 3 条）：")
        for p in prompts[:3]:
            print(f"  [{p['id']}] ({p['aspect']}) {p['prompt'][:70]}...")
        return 0

    if remaining <= 0:
        print("\n全部 prompt 都已生成过。要用新的一批请换文件，或 --reset 重新从头跑。")
        return 0

    n = args.n if args.n and args.n > 0 else _ask_n(10)
    n = min(n, remaining)
    selected = prompts[next_index: next_index + n]

    default_batch = f"{inp.stem}_out"
    if args.folder:
        batch_dir = _clean_dirname(args.folder) or default_batch
    elif args.dry_run:
        batch_dir = default_batch
    else:
        batch_dir = _ask_folder(default_batch)
    outdir = root / batch_dir

    models = [m.strip() for m in (args.model or "").split(",") if m.strip()]
    if not models:
        models = FAST_MODELS if args.fast else DEFAULT_MODELS

    print(f"\n本次生成 : {len(selected)} 张（第 {next_index + 1} ~ {next_index + len(selected)} 条）")
    print(f"存放位置  : {outdir}")
    print(f"模型链    : {' -> '.join(models)}")
    print(f"并发/超时 : {args.concurrency} / {args.timeout}s")
    if args.dry_run:
        print("--dry-run：仅预览，不消耗额度、不写进度。\n")
        for p in selected:
            print(f"  - {p['id']}  [{p['aspect']}]  {p['prompt'][:80]}...")
        return 0

    # 出图前检查登录状态：未登录直接拦截（否则 headless 会挂住等超时）
    # 用 --api-key 时无需 OAuth 登录
    state, logged = gemini_login_state()
    if args.api_key:
        logged = True
    if not logged and not args.force:
        print(f"\n[已拦截] {state}")
        print("Gemini CLI 还没登录 Google 账号，生图会直接卡死。请先完成一次性登录：\n")
        print("  1) 打开任意命令行，执行：  gemini")
        print("  2) 选「Sign in with Google」，浏览器弹出后登录你的 Gemini Pro 账号")
        print("  3) 登录成功后关掉终端，再回来跑本脚本\n")
        print("  确认已登录后重跑即可。若你确有把握已登录，可加 --force 跳过此检查。")
        return 1
    if not logged:
        print(f"\n  [警告] {state}，按 --force 继续尝试…\n")

    outdir.mkdir(parents=True, exist_ok=True)

    lock = threading.Lock()
    counter = {"done": 0, "ok": 0, "fail": 0, "cached": 0}

    print(f"\n开始出图 {iso(time.time())}（并发 {args.concurrency}，实时进度见下）\n")
    t0 = time.time()
    results: list[dict] = []
    with ThreadPoolExecutor(max_workers=args.concurrency) as ex:
        futs = {ex.submit(run_one, p, args, outdir, models, lock, counter): p
                for p in selected}
        for fut in as_completed(futs):
            results.append(fut.result())

    # 推进度：从本次切片头部数起，连续成功几张就前进几条；遇到第一张失败的停下
    by_id = {r["id"]: r for r in results}
    advanced = 0
    for p in selected:
        r = by_id.get(p["id"])
        if r and r["status"] in ("ok", "cached"):
            advanced += 1
        else:
            break
    new_index = next_index + advanced
    save_progress(prog_file, inp, total, new_index)

    failed = [r for r in results if r["status"] == "fail"]
    elapsed = round(time.time() - t0, 1)
    print(f"\n{'=' * 60}")
    print(f"本次完成 : 成功 {counter['ok'] + counter['cached']}/{len(selected)} 张"
          + (f"（含续跑跳过 {counter['cached']} 张）" if counter["cached"] else ""))
    if failed:
        print(f"失败 {len(failed)} 张（下次会从失败的那条开始重试）：")
        for r in failed:
            print(f"  - {r['id']}  {r['error']}")
    print(f"耗时     : {elapsed}s")
    print(f"总进度   : 已生成 {new_index}/{total} 条"
          + (f"，下次从第 {new_index + 1} 条继续" if new_index < total else "，全部完成"))
    print(f"进度文件 : {prog_file}")
    print(f"成品目录 : {outdir}")
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
