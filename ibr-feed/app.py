import hashlib
import hmac
import logging
import os
import subprocess
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

import markdown
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import Response
from feedgen.feed import FeedGenerator

DATA_DIR = Path(os.getenv("IBR_DATA_DIR", "/data"))
REPO_URL = os.getenv("IBR_REPO_URL", "https://github.com/gdshadow01/Vergaberechtsprechung.git")
BRANCH = os.getenv("IBR_BRANCH", "main")
SYNC_INTERVAL = int(os.getenv("IBR_SYNC_INTERVAL", "900"))
PAT = os.getenv("IBR_GITHUB_PAT", "")
FEED_TOKEN = os.getenv("IBR_FEED_TOKEN", "")
REPO_DIR = DATA_DIR / "repo"
FEED_FILE = DATA_DIR / "leitsaetze.atom"
app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)


def _git(args, cwd=None):
    env = os.environ.copy()
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    helper = DATA_DIR / "git-askpass.sh"
    helper.write_text('#!/bin/sh\ncase "$1" in *sername*) echo x ;; *) echo "$IBR_GITHUB_PAT" ;; esac\n')
    helper.chmod(0o700)
    env.update(GIT_ASKPASS=str(helper), GIT_TERMINAL_PROMPT="0")
    env["IBR_GITHUB_PAT"] = PAT
    return subprocess.run(["git", *args], cwd=cwd, env=env, check=True, capture_output=True, text=True).stdout.strip()


def _dates(relpath):
    log = _git(["log", "--follow", "--format=%aI", "--", relpath], REPO_DIR).splitlines()
    if not log:
        raise ValueError(f"No history for {relpath}")
    first = _git(["log", "--follow", "--reverse", "--format=%aI", "--", relpath], REPO_DIR).splitlines()[0]
    updated = _git(["log", "-1", "--format=%cI", "--", relpath], REPO_DIR)
    return first, updated


def _parse(path):
    text = path.read_text(encoding="utf-8")
    title = path.stem
    if text.startswith("---\n"):
        _, frontmatter, body = text.split("---", 2)
        for line in frontmatter.splitlines():
            if line.startswith("title:"):
                title = line.split(":", 1)[1].strip().strip("\"'")
                break
        body = body.lstrip("\n")
        lines = body.splitlines()
        if lines and lines[0].strip().lstrip("# ") == title:
            body = "\n".join(lines[1:]).lstrip("\n")
    else:
        lines = text.splitlines()
        first = next((i for i, line in enumerate(lines) if line.strip()), None)
        body = ""
        if first is not None:
            title = lines[first].strip().lstrip("# ")
            body = "\n".join(lines[first + 1:]).lstrip("\n")
    return title, markdown.markdown(body, extensions=["extra"])


def build_feed():
    paths = sorted((REPO_DIR / "Leitsätze").glob("*.md"))
    entries = []
    for path in paths:
        relpath = f"Leitsätze/{path.name}"
        published, updated = _dates(relpath)
        title, content = _parse(path)
        entries.append((updated, path, published, title, content))
    fg = FeedGenerator()
    fg.id("urn:ibr-leitsaetze:feed")
    fg.title("IBR Leitsätze — Vergaberechtsprechung")
    fg.link(href="http://ibr-feed:8000/", rel="self")
    fg.updated(max((datetime.fromisoformat(row[0]) for row in entries), default=datetime(1970, 1, 1, tzinfo=timezone.utc)))
    for updated, path, published, title, content in sorted(entries, key=lambda row: (row[0], row[1].name), reverse=True):
        entry = fg.add_entry()
        entry.id(f"urn:ibr-leitsaetze:{path.stem}")
        entry.title(title)
        entry.link(href=f"https://github.com/gdshadow01/Vergaberechtsprechung/blob/main/Leits%C3%A4tze/{quote(path.name)}")
        entry.published(datetime.fromisoformat(published))
        entry.updated(datetime.fromisoformat(updated))
        entry.content(content, type="html")
        entry.author(name=title.split(",", 1)[0])
    return fg.atom_str(pretty=False)


def sync_once():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if REPO_DIR.exists():
        _git(["fetch", "origin", BRANCH], REPO_DIR)
        _git(["checkout", "-f", f"origin/{BRANCH}"], REPO_DIR)
        _git(["reset", "--hard", f"origin/{BRANCH}"], REPO_DIR)
    else:
        _git(["clone", "--branch", BRANCH, REPO_URL, str(REPO_DIR)])
    data = build_feed()
    tmp = FEED_FILE.with_suffix(".tmp")
    tmp.write_bytes(data)
    tmp.replace(FEED_FILE)


def _sync_loop():
    while True:
        try:
            sync_once()
        except Exception:
            logging.exception("IBR feed sync failed")
        time.sleep(SYNC_INTERVAL)


@app.on_event("startup")
def startup():
    if not PAT or not FEED_TOKEN:
        raise RuntimeError("IBR_GITHUB_PAT and IBR_FEED_TOKEN are required")
    threading.Thread(target=_sync_loop, daemon=True).start()


@app.get("/healthz")
def healthz():
    return {"status": "ok"}


@app.get("/{token}/leitsaetze.atom")
def atom(token: str, request: Request):
    if not hmac.compare_digest(hashlib.sha256(token.encode()).digest(), hashlib.sha256(FEED_TOKEN.encode()).digest()):
        raise HTTPException(status_code=404)
    if not FEED_FILE.exists():
        raise HTTPException(status_code=503)
    return Response(FEED_FILE.read_bytes(), media_type="application/atom+xml")


@app.middleware("http")
async def redact_paths(request: Request, call_next):
    return await call_next(request)
