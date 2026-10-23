# Gemini Web Batch Image Generation (No API Key)

Batch-generate images with your own **gemini.google.com account** — the same account
you use to generate images in the browser. **No API key required, no deprecated
CLI OAuth.** It reuses your logged-in Google session cookies and drives the Gemini
web backend directly.

> Use case: building rich-text / document image datasets on your own subscription quota.

## How it works

The script is built on [`gemini_webapi`](https://github.com/HanaokaYuzu/Gemini-API),
a reverse-engineered Python wrapper for the Gemini web app. Authentication relies on
two cookies:

- `__Secure-1PSID` — the main session credential, long-lived
- `__Secure-1PSIDTS` — a short-lived session timestamp, auto-refreshed by the library

Because it talks to the **same backend** as clicking "Generate image" in the browser,
**anything you can generate on the web, you can generate here.**

> Background: on 2026-06-18 Google shut down the Gemini CLI's OAuth login (only the
> coding-oriented Code Assist tier remains). The old "CLI OAuth without an API key"
> route is dead; this project uses the web interface instead.

## Requirements

- Python 3.11+
- A Google account that can generate images, signed in at gemini.google.com

## Quick start

### 1. Install dependencies

```bash
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
```

On Windows you can also just double-click `setup_web.bat`.

### 2. Get your cookies

1. Sign in to https://gemini.google.com
2. Press **F12** → **Application** → **Cookies** → click `https://gemini.google.com`
3. Copy the **Value** of `__Secure-1PSID` and `__Secure-1PSIDTS`
4. Save them locally (git-ignored, never committed):

```bash
copy cookies.json.example cookies.json
# edit cookies.json and paste the two values
```

Or store them directly:

```bash
.venv\Scripts\python run_gemini_web.py --cookie-1psid "VALUE1" --cookie-1psidts "VALUE2" --save-cookies
```

### 3. Verify and generate

```bash
.venv\Scripts\python run_gemini_web.py --check          # verify cookies / connection
.venv\Scripts\python run_gemini_web.py --input prompts.csv --n 10
```

## Input format

Identical to the `run_my_prompts.py` (codex) contract — existing prompt files work as-is:

- **CSV**: at least a `prompt` column; optional `sample_id` / `aspect_ratio` columns
- **TXT**: one prompt per line; `#` comments and blank lines are ignored
- **JSONL**: `{id, prompt, aspect}` per line

Aspect ratios: `1:1` `3:2` `2:3` `16:9` `9:16` `4:3` `3:4`

## Command-line options

| Option | Description | Default |
|---|---|---|
| `--input` | prompt file (.csv/.txt/.jsonl) | — |
| `--n` | number of images this run | interactive |
| `--check` | verify cookies / connection | — |
| `--list-models` | list the account's available models | — |
| `--model` | model name/alias to use | account default |
| `--dry-run` | preview only, no quota | — |
| `--reset` / `--start` | restart / start at index | — |
| `--delay` | seconds between images (rate-limit guard) | 3 |
| `--retries` | retries on failure | 2 |
| `--proxy` | HTTP proxy, e.g. `http://127.0.0.1:7890` | inherited |
| `--default-aspect` | default aspect for TXT | 1:1 |
| `--cookie-1psid` / `--cookie-1psidts` | pass cookies via CLI | — |
| `--save-cookies` | persist cookies to cookies.json | — |

## Resume support

Progress is stored in `output/_progress/<stem>.progress.json`, keyed to the prompt
file. Re-run the same command after an interruption and it continues from where it
left off — no duplicates, no gaps.

## Output

```
output/<file>_out/
├── <id>.png            # final image, normalized to PNG, named by sample_id
└── _work/<id>/         # per-image temp download dir (safe to delete)
```

## Cookie notes

- **`__Secure-1PSID` is long-lived** — keep it valid by not signing out, not changing
  your password, and not clearing browser cookies.
- **`__Secure-1PSIDTS` is auto-refreshed** (via `auto_refresh`), with the refreshed
  cache stored in `.cookie_cache/` under the script directory.
- If cookies keep expiring within a few hours, it is likely Chrome/Edge "Device Bound
  Session Credentials" (DBSC). **Use Firefox to sign in and extract cookies** for
  long-lived, renewable sessions.

## FAQ

| Symptom | Fix |
|---|---|
| `No cookie found` | Follow "Get your cookies", or run `--save-cookies` once |
| `Init failed` / cookie expired | Re-sign in to gemini.google.com and update cookies.json |
| Download error `Redirect to internal IP ... SSRF` | Already patched (SSRF protection disabled on the download session); if it persists, check the printed image URL |
| `No image generated (safety-blocked)` | Try another prompt or simplify the description |
| Repeated 429 rate limits | Increase `--delay`, resume later |

## Disclaimer

This project drives the Gemini web interface by reusing **your own account's** session
cookies. It may violate Google's Terms of Service and may break if Google changes its
internal APIs. Use it only with your own account, keep request rates low, and proceed
at your own risk.
