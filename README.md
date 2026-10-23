# Gemini Web 批量生图（免 API key）

用你自己的 **gemini.google.com 账号**（就是网页版能生图的账号）批量生成图片。
**不申请 API key、不走已废弃的 Gemini CLI OAuth**，直接复用浏览器里已登录的
Google 会话 Cookie，走网页版那套后端。

> 适合场景：构建富文本/文档类图像数据集时，用自己订阅额度批量出图。

## 原理

脚本基于 [`gemini_webapi`](https://github.com/HanaokaYuzu/Gemini-API)（逆向
Gemini 网页版接口的 Python 库），认证靠两个 Cookie：

- `__Secure-1PSID` —— 主会话凭证，长期有效
- `__Secure-1PSIDTS` —— 会话时间戳，短期，库会自动续期

因为和你在浏览器里点「生成图片」走的是**同一套后端**，所以**网页能生图，这里就能生图**。

> ⚠️ 背景：2026-06-18 起 Google 停用了 Gemini CLI 的 OAuth 登录（只剩编程用
> Code Assist 档），所以以前「CLI OAuth 免 key 生图」的路线已失效，本项目改走
> 网页版接口。

## 环境要求

- Python 3.11+
- 一个能生图的 Google 账号（已在 gemini.google.com 登录）

## 快速开始

### 1. 装依赖

```bash
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
```

Windows 也可以直接双击 `setup_web.bat`。

### 2. 拿 Cookie

1. 浏览器登录 https://gemini.google.com
2. 按 **F12** → **Application（应用）** → **Cookies** → 点 `https://gemini.google.com`
3. 复制 **`__Secure-1PSID`** 和 **`__Secure-1PSIDTS`** 的「值」
4. 存到本地（会被 `.gitignore` 忽略，不会提交）：

```bash
copy cookies.json.example cookies.json
# 用编辑器打开 cookies.json，把两个值填进去
```

或直接用命令存：

```bash
.venv\Scripts\python run_gemini_web.py --cookie-1psid "值1" --cookie-1psidts "值2" --save-cookies
```

### 3. 验证 + 出图

```bash
.venv\Scripts\python run_gemini_web.py --check          # 验证 Cookie / 连接
.venv\Scripts\python run_gemini_web.py --input 证据文件.csv --n 10
```

## 输入格式

与 `run_my_prompts.py`（codex 版）完全一致，`document_prompts_unified.csv` /
`证据文件.csv` 可直接复用：

- **CSV**：至少一列 `prompt`，可选 `sample_id` / `aspect_ratio` 列
- **TXT**：每行一条 prompt，`#` 开头和空行忽略
- **JSONL**：`{id, prompt, aspect}` 一行一条

宽高比支持：`1:1` `3:2` `2:3` `16:9` `9:16` `4:3` `3:4`

## 命令行参数

| 参数 | 说明 | 默认 |
|---|---|---|
| `--input` | prompt 文件（.csv/.txt/.jsonl） | — |
| `--n` | 本次生成张数 | 交互询问 |
| `--check` | 验证 Cookie / 连接 | — |
| `--list-models` | 列出账号可用模型 | — |
| `--model` | 指定生图模型名/别名 | 账号默认 |
| `--dry-run` | 只预览，不花额度 | — |
| `--reset` / `--start` | 从头 / 从指定序号开始 | — |
| `--delay` | 每张间隔秒数（防限流） | 3 |
| `--retries` | 失败重试次数 | 2 |
| `--proxy` | HTTP 代理，如 `http://127.0.0.1:7890` | 继承终端代理 |
| `--default-aspect` | TXT 缺省比例 | 1:1 |
| `--cookie-1psid` / `--cookie-1psidts` | 命令行传 Cookie | — |
| `--save-cookies` | 把传入 Cookie 存到 cookies.json | — |

## 断点续跑

进度存在 `output/_progress/<文件名>.progress.json`，与 prompt 文件绑定。
中断/断电后重跑同一条命令，会自动从上次停下的位置继续，不重复、不遗漏。

## 产物

```
output/<文件>_out/
├── <id>.png            # 成品图（统一转 PNG，按 sample_id 命名）
└── _work/<id>/         # 每张的临时下载目录（可删）
```

## Cookie 说明

- **`__Secure-1PSID` 长期有效**，只要不退出登录 / 改密码 / 清浏览器 Cookie。
- **`__Secure-1PSIDTS` 由库自动续期**（已开启 `auto_refresh`），续期缓存存在
  脚本目录 `.cookie_cache/`，下次运行自动读最新值，无需手动更新。
- 若 Cookie **几小时就失效**，多半是 Chrome/Edge 的「设备绑定凭证」(DBSC) 导致，
  **改用 Firefox 登录取 Cookie** 即可长期有效。

## 常见问题

| 现象 | 处理 |
|---|---|
| `没找到 Cookie` | 按「拿 Cookie」步骤获取，或 `--save-cookies` 存一次 |
| `初始化失败` / Cookie 过期 | 重新登录 gemini.google.com，更新 cookies.json |
| 下载报 `Redirect to internal IP ... SSRF` | 已内置修复（关闭下载会话的 SSRF 防护）；若仍报，看打印的图片 URL |
| `没生成图（安全策略拦截）` | 换一条 prompt 或简化描述 |
| 连续 429 限流 | 调大 `--delay`，过几小时再续跑 |

## 免责声明

本项目通过逆向 Gemini 网页接口、复用你本人账号的会话 Cookie 来生图，**可能
违反 Google 服务条款，接口也可能随时变化而失效**。请仅用于你自己的账号、
控制调用频率，风险自负。
