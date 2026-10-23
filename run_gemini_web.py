#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
run_gemini_web.py — 用 Gemini 网页版账号（Cookie）批量生图，免 API key

原理：逆向 Gemini 网页版（gemini.google.com）的接口，认证靠浏览器里已登录
Google 会话的 Cookie（__Secure-1PSID / __Secure-1PSIDTS），走你**网页版能生图**
的那套后端。不需要 API key，也不需要已经废弃的 Gemini CLI OAuth 登录。

依赖（已装进本目录 .venv）：gemini_webapi + Pillow
运行方式：双击 run_gemini_web.bat，或
    .venv\\Scripts\\python.exe run_gemini_web.py --input 文件 --n 张数

────────────────────────────────────────────────────────────
第一次用：先拿 Cookie（关键，只做一次）
────────────────────────────────────────────────────────────
1. 浏览器登录 https://gemini.google.com（用你能生图的账号）
2. 按 F12 → Application（应用）→ Cookies → https://gemini.google.com
3. 复制 __Secure-1PSID 和 __Secure-1PSIDTS 两个值
4. 跑一次下面命令，把 Cookie 存起来：
     .venv\\Scripts\\python.exe run_gemini_web.py --cookie-1psid 值1 --cookie-1psidts 值2 --save-cookies
   之后就不用再传了（存在同目录 cookies.json）。

────────────────────────────────────────────────────────────
输入格式（与 run_my_prompts.py 完全一致）
────────────────────────────────────────────────────────────
· CSV：至少一列「prompt」，可带 sample_id / aspect_ratio 列
· TXT：每行一条，# 开头和空行忽略
· JSONL：{id, prompt, aspect} 一行一条

常用命令：
  --check                只验证 Cookie 和账号，不花额度
  --list-models          列出该账号可用的模型
  --dry-run --n 5        只预览要生成哪些，不花额度
  --n 50                 本次生成 50 张
  --reset                从头开始
  --model 模型名         指定生图模型（默认用账号默认，网页版自动路由）
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import hashlib
import io
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
COOKIE_FILE = HERE / "cookies.json"
# Cookie 自动续期的缓存目录：默认在系统临时目录（重启可能被清），改到脚本目录下更持久
os.environ.setdefault("GEMINI_COOKIE_PATH", str(HERE / ".cookie_cache"))

VALID_ASPECTS = {"1:1", "3:2", "2:3", "16:9", "9:16", "4:3", "3:4"}
DEFAULT_ASPECT = "1:1"
ASPECT_HINT = {
    "1:1":  "1:1（正方形）",
    "3:2":  "3:2（横版）",
    "2:3":  "2:3（竖版）",
    "16:9": "16:9（宽屏横版）",
    "9:16": "9:16（竖屏）",
    "4:3":  "4:3（横版）",
    "3:4":  "3:4（竖版）",
}

SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
WIN_RESERVED = {"con", "prn", "aux", "nul", *(f"com{i}" for i in range(1, 10)),
                *(f"lpt{i}" for i in range(1, 10))}


def iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).astimezone().isoformat(timespec="seconds")


def _read_text_auto(path: Path) -> str:
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

def _pick_col(headers, candidates):
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
    rows, seen = [], set()
    reader = csv.DictReader(io.StringIO(_read_text_auto(path)))
    headers = reader.fieldnames or []
    id_col = _pick_col(headers, ("sample_id", "id", "编号", "image_id"))
    prompt_col = _pick_col(headers, ("prompt", "prompt_text", "text", "描述", "内容"))
    aspect_col = _pick_col(headers, ("aspect_ratio", "aspect", "ratio", "比例", "宽高比"))
    if not prompt_col:
        raise SystemExit(f"[错误] CSV 里找不到「prompt」列。表头：{headers}")
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
    rows = []
    for i, line in enumerate(_read_text_auto(path).splitlines(), 1):
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        rows.append({"id": f"p-{i:06d}", "prompt": s, "aspect": DEFAULT_ASPECT})
    return rows


def _load_jsonl(path: Path) -> list[dict]:
    rows = []
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
        raise SystemExit(f"[错误] {path.name} 里没有读到任何 prompt。")
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
            return max(0, min(int(d.get("next_index", 0)), total))
        print("  [提示] prompt 文件已变化（或进度损坏），从 0 重新开始。", file=sys.stderr)
    return 0


def save_progress(prog_file: Path, input_path: Path, total: int, next_index: int) -> None:
    d = {"input": str(input_path.resolve()), "input_sha": _file_sha(input_path),
         "total": total, "next_index": next_index, "updated_at": iso(time.time())}
    prog_file.parent.mkdir(parents=True, exist_ok=True)
    tmp = prog_file.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, prog_file)


# ---------------------------------------------------------------- PNG 校验

def png_ok(path: Path) -> bool:
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


# ---------------------------------------------------------------- Cookie 管理

def load_cookies(args) -> tuple[str, str]:
    psid, psidts = args.cookie_1psid, args.cookie_1psidts
    # 兜底：读 cookies.json
    if not psid and COOKIE_FILE.exists():
        try:
            d = json.loads(COOKIE_FILE.read_text(encoding="utf-8"))
            psid = psid or d.get("__Secure-1PSID", "")
            psidts = psidts or d.get("__Secure-1PSIDTS", "")
        except (json.JSONDecodeError, OSError):
            pass
    # 再兜底：环境变量
    psid = psid or os.environ.get("GEMINI_1PSID", "")
    psidts = psidts or os.environ.get("GEMINI_1PSIDTS", "")
    return psid, psidts


def save_cookies_file(psid: str, psidts: str) -> None:
    COOKIE_FILE.write_text(
        json.dumps({"__Secure-1PSID": psid, "__Secure-1PSIDTS": psidts,
                    "saved_at": iso(time.time())}, ensure_ascii=False, indent=2),
        encoding="utf-8")
    print(f"Cookie 已保存到 {COOKIE_FILE}")


# ---------------------------------------------------------------- 生图核心

def build_prompt(task: dict) -> str:
    hint = ASPECT_HINT.get(task.get("aspect", DEFAULT_ASPECT), task.get("aspect", DEFAULT_ASPECT))
    return f"生成一张图片，宽高比 {hint}。图片内容：\n{task['prompt']}"


async def generate_one(client, dl_client, task: dict, args, outdir: Path,
                       lock: asyncio.Lock, counter: dict) -> dict:
    from PIL import Image as PILImage
    pid = task["id"]
    final = outdir / f"{pid}.png"
    if png_ok(final):
        async with lock:
            counter["done"] += 1
            counter["cached"] += 1
            print(f"[cached] {pid}", flush=True)
        return {"id": pid, "status": "cached", "error": ""}

    workdir = outdir / "_work" / pid
    workdir.mkdir(parents=True, exist_ok=True)

    prompt = build_prompt(task)
    last_err = ""
    for attempt in range(1, args.retries + 2):
        try:
            resp = await client.generate_content(prompt, model=args.model or None,
                                                 temporary=True)
        except Exception as e:
            last_err = f"{type(e).__name__}: {str(e)[:180]}"
            await asyncio.sleep(min(8 * attempt, 60))
            continue
        images = getattr(resp, "images", None) or []
        if not images:
            txt = (getattr(resp, "text", "") or "")[:120]
            last_err = "没生成图" + (f"（模型回复：{txt}）" if txt else "（可能被安全策略拦截）")
            await asyncio.sleep(min(8 * attempt, 60))
            continue
        try:
            # 用关掉 SSRF 防护的下载会话（图片 CDN 会跳内网 IP，被默认 SAFE 拦截）
            saved = await images[0].save(path=str(workdir), filename=f"{pid}.png",
                                         client=dl_client)
        except Exception as e:
            url = getattr(images[0], "url", "?")
            last_err = f"下载失败: {type(e).__name__}: {str(e)[:140]}"
            print(f"  [debug] 图片URL: {url[:120]}", flush=True)
            await asyncio.sleep(min(8 * attempt, 60))
            continue
        # 用 Pillow 统一转成标准 PNG（网页版可能返回 JPEG）
        try:
            im = PILImage.open(saved)
            im = im.convert("RGB")
            im.save(final, "PNG")
            if png_ok(final):
                async with lock:
                    counter["done"] += 1
                    counter["ok"] += 1
                    print(f"[OK ] {pid}", flush=True)
                return {"id": pid, "status": "ok", "error": ""}
            last_err = "转 PNG 后校验失败"
        except Exception as e:
            last_err = f"图片处理失败: {type(e).__name__}: {str(e)[:160]}"

    async with lock:
        counter["done"] += 1
        counter["fail"] += 1
        print(f"[FAIL] {pid}  {last_err}", flush=True)
    return {"id": pid, "status": "fail", "error": last_err}


async def run_batch(client, dl_client, selected: list[dict], args, outdir: Path) -> list[dict]:
    lock = asyncio.Lock()
    counter = {"done": 0, "ok": 0, "fail": 0, "cached": 0}

    results = []
    for i, task in enumerate(selected):
        results.append(await generate_one(client, dl_client, task, args, outdir, lock, counter))
        if args.delay > 0 and i < len(selected) - 1:
            await asyncio.sleep(args.delay)
    return results


# ---------------------------------------------------------------- 检查 / 列模型

async def do_check(args) -> int:
    from gemini_webapi import GeminiClient
    psid, psidts = load_cookies(args)
    if not psid:
        print("[错误] 没找到 Cookie。请按脚本顶部说明获取 __Secure-1PSID / __Secure-1PSIDTS，"
              "或用 --cookie-1psid 传入。")
        return 1
    client = GeminiClient(psid, psidts, proxy=args.proxy or None)
    print("正在连接 gemini.google.com 验证 Cookie…")
    try:
        await client.init(timeout=args.init_timeout, auto_close=False,
                          close_delay=300, auto_refresh=True)
    except Exception as e:
        print(f"[失败] 连接/初始化失败：{type(e).__name__}: {str(e)[:200]}")
        print("  Cookie 可能已过期或格式不对，请重新登录 gemini.google.com 再复制。")
        return 1
    print("[OK] Cookie 有效，账号连接正常。")
    if args.list_models or True:
        models = client.list_models()
        if models:
            print(f"\n该账号可用模型（{len(models)} 个）：")
            for m in models:
                mark = "✓" if m.is_available else "✗"
                print(f"  [{mark}] {m.display_name or m.model_name}  "
                      f"(id={m.model_id}, 别名={','.join(m.aliases) or '-'})")
    await client.close()
    return 0


# ---------------------------------------------------------------- 交互

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
        raw = input(f"是否新建文件夹存放这批图？(y/n，默认 n，放「{default}」)：").strip().lower()
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
            print("  文件夹名不能为空。")
    return default


# ---------------------------------------------------------------- main

def main() -> int:
    _fix_console()
    ap = argparse.ArgumentParser(description="用 Gemini 网页版账号（Cookie）批量生图，免 API key")
    ap.add_argument("--input", help="prompt 文件：.csv / .txt / .jsonl")
    ap.add_argument("--output-dir", default="", help="输出根目录，默认本目录 output/")
    ap.add_argument("--folder", default="", help="本次输出子文件夹名")
    ap.add_argument("--n", type=int, default=0, help="本次张数；0=交互询问")
    ap.add_argument("--start", type=int, default=None, help="起始序号（0 起）")
    ap.add_argument("--reset", action="store_true", help="从头开始")
    ap.add_argument("--default-aspect", default="1:1",
                    help="TXT/缺省宽高比，可选 %s" % ",".join(sorted(VALID_ASPECTS)))
    ap.add_argument("--list", action="store_true", help="只列出文件概况")
    ap.add_argument("--dry-run", action="store_true", help="只预览，不花额度")
    ap.add_argument("--check", action="store_true", help="验证 Cookie / 连接")
    ap.add_argument("--list-models", action="store_true", help="连接后列出账号可用模型")
    ap.add_argument("--model", default="", help="生图模型名/别名；默认账号默认模型")
    ap.add_argument("--cookie-1psid", default="", help="__Secure-1PSID 值")
    ap.add_argument("--cookie-1psidts", default="", help="__Secure-1PSIDTS 值")
    ap.add_argument("--save-cookies", action="store_true", help="把传入的 Cookie 存到 cookies.json")
    ap.add_argument("--proxy", default="", help="HTTP 代理，如 http://127.0.0.1:7890")
    ap.add_argument("--delay", type=float, default=3.0, help="每张间隔秒数，防限流")
    ap.add_argument("--retries", type=int, default=2, help="失败重试次数")
    ap.add_argument("--init-timeout", type=int, default=30, help="初始化超时秒数")
    args = ap.parse_args()

    # --save-cookies：只存 Cookie 就退出
    if args.save_cookies:
        psid, psidts = load_cookies(args)
        if not psid:
            raise SystemExit("[错误] 没拿到 Cookie，无法保存。请用 --cookie-1psid 传入。")
        save_cookies_file(psid, psidts)
        return 0

    # --check / --list-models
    if args.check or args.list_models:
        return asyncio.run(do_check(args))

    if not args.input:
        raise SystemExit("请用 --input 指定 prompt 文件（或 --check 验证 Cookie）。")

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
        print("\n全部 prompt 都已生成过。换文件或 --reset。")
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

    print(f"\n本次生成 : {len(selected)} 张（第 {next_index + 1} ~ {next_index + len(selected)} 条）")
    print(f"存放位置  : {outdir}")
    print(f"模型      : {args.model or '账号默认（网页版自动路由生图）'}")
    print(f"间隔/重试 : {args.delay}s / {args.retries} 次")
    if args.dry_run:
        print("--dry-run：仅预览，不花额度。\n")
        for p in selected:
            print(f"  - {p['id']}  [{p['aspect']}]  {p['prompt'][:80]}...")
        return 0

    psid, psidts = load_cookies(args)
    if not psid:
        print("\n[错误] 没找到 Cookie。请先获取 __Secure-1PSID / __Secure-1PSIDTS，"
              "或先跑 --check 验证。")
        return 1

    outdir.mkdir(parents=True, exist_ok=True)
    print(f"\n开始出图 {iso(time.time())}\n")

    from gemini_webapi import GeminiClient
    client = GeminiClient(psid, psidts, proxy=args.proxy or None)

    async def _run():
        try:
            await client.init(timeout=args.init_timeout, auto_close=False,
                              close_delay=300, auto_refresh=True)
        except Exception as e:
            print(f"[失败] 初始化失败：{type(e).__name__}: {str(e)[:200]}")
            print("  Cookie 可能过期，请重新登录 gemini.google.com 后更新 Cookie。")
            return None
        # 下载图片专用会话：关掉 SSRF 防护（图片 CDN 会跳内网 IP，默认 SAFE 会被拦）
        from curl_cffi.requests import AsyncSession
        dl_client = AsyncSession(
            cookies=client.cookies, proxy=client.proxy or None,
            impersonate=client.impersonate, allow_redirects=True, timeout=120)
        try:
            return await run_batch(client, dl_client, selected, args, outdir)
        finally:
            try:
                await dl_client.close()
            except Exception:
                pass

    t0 = time.time()
    results = asyncio.run(_run())

    async def _close():
        try:
            await client.close()
        except Exception:
            pass
    asyncio.run(_close())

    if results is None:
        return 1

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

    ok = sum(1 for r in results if r["status"] in ("ok", "cached"))
    failed = [r for r in results if r["status"] == "fail"]
    print(f"\n{'=' * 60}")
    print(f"本次完成 : 成功 {ok}/{len(selected)} 张")
    if failed:
        print(f"失败 {len(failed)} 张（下次从失败那条重试）：")
        for r in failed:
            print(f"  - {r['id']}  {r['error']}")
    print(f"耗时     : {round(time.time() - t0, 1)}s")
    print(f"总进度   : 已生成 {new_index}/{total} 条"
          + (f"，下次从第 {new_index + 1} 条继续" if new_index < total else "，全部完成"))
    print(f"成品目录 : {outdir}")
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
