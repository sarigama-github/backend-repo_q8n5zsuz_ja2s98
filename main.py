import os
from typing import List, Optional

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import requests

from database import db, create_document, get_documents

app = FastAPI(title="LTD SaaS Ingestor API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


class SyncRequest(BaseModel):
    repo_url: str
    branch: Optional[str] = None
    token: Optional[str] = None


@app.get("/")
def root():
    return {"status": "ok", "service": "backend", "name": "LTD SaaS Ingestor"}


@app.get("/test")
def test_database():
    response = {
        "backend": "✅ Running",
        "database": "❌ Not Available",
        "database_url": None,
        "database_name": None,
        "connection_status": "Not Connected",
        "collections": []
    }

    try:
        if db is not None:
            response["database"] = "✅ Available"
            response["database_url"] = "✅ Set" if os.getenv("DATABASE_URL") else "❌ Not Set"
            response["database_name"] = os.getenv("DATABASE_NAME") or "❌ Not Set"
            response["connection_status"] = "Connected"
            try:
                collections = db.list_collection_names()
                response["collections"] = collections[:10]
                response["database"] = "✅ Connected & Working"
            except Exception as e:
                response["database"] = f"⚠️  Connected but Error: {str(e)[:120]}"
        else:
            response["database"] = "⚠️  Available but not initialized"
    except Exception as e:
        response["database"] = f"❌ Error: {str(e)[:120]}"

    return response


# Utility: simple language detection by extension
EXT_LANG = {
    ".py": "python",
    ".js": "javascript",
    ".jsx": "javascript",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".json": "json",
    ".md": "markdown",
    ".yml": "yaml",
    ".yaml": "yaml",
    ".html": "html",
    ".css": "css",
    ".env": "env",
}


def detect_language(path: str) -> Optional[str]:
    for ext, lang in EXT_LANG.items():
        if path.lower().endswith(ext):
            return lang
    return None


# GitHub API helpers
GITHUB_API = "https://api.github.com"


def parse_repo_url(repo_url: str):
    # supports forms like https://github.com/owner/name and owner/name
    if repo_url.startswith("http"):
        parts = repo_url.rstrip("/").split("github.com/")[-1].split("/")
    else:
        parts = repo_url.split("/")
    if len(parts) < 2:
        raise HTTPException(status_code=400, detail="Invalid GitHub repository URL")
    owner, name = parts[0], parts[1]
    return owner, name


def github_headers(token: Optional[str]):
    headers = {"Accept": "application/vnd.github+json"}
    token = token or os.getenv("GITHUB_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


@app.post("/api/sync")
def sync_repository(payload: SyncRequest):
    owner, name = parse_repo_url(payload.repo_url)
    full_name = f"{owner}/{name}"

    # Get default branch
    repo_resp = requests.get(f"{GITHUB_API}/repos/{owner}/{name}", headers=github_headers(payload.token))
    if repo_resp.status_code != 200:
        raise HTTPException(status_code=repo_resp.status_code, detail=repo_resp.text)
    repo_data = repo_resp.json()
    default_branch = payload.branch or repo_data.get("default_branch", "main")

    # Get tree recursively
    ref_resp = requests.get(
        f"{GITHUB_API}/repos/{owner}/{name}/git/trees/{default_branch}?recursive=1",
        headers=github_headers(payload.token),
    )
    if ref_resp.status_code != 200:
        raise HTTPException(status_code=ref_resp.status_code, detail=ref_resp.text)
    tree = ref_resp.json().get("tree", [])

    # Save repo metadata
    from schemas import Repo, FileDocument
    repo_doc = Repo(
        owner=owner,
        name=name,
        full_name=full_name,
        url=payload.repo_url,
        default_branch=default_branch,
        description=repo_data.get("description"),
    )
    create_document("repo", repo_doc)

    saved = 0
    for node in tree:
        if node.get("type") == "blob":
            path = node.get("path")
            sha = node.get("sha")
            size = node.get("size")
            # Fetch blob
            blob_resp = requests.get(
                f"{GITHUB_API}/repos/{owner}/{name}/contents/{path}?ref={default_branch}",
                headers=github_headers(payload.token),
            )
            if blob_resp.status_code != 200:
                continue
            blob = blob_resp.json()
            encoding = blob.get("encoding")
            content = None
            if encoding == "base64":
                # Keep base64 to avoid decoding errors for binaries
                content = blob.get("content")
            else:
                content = blob.get("content")

            doc = FileDocument(
                repo_full_name=full_name,
                path=path,
                sha=sha,
                size=size,
                type="blob",
                content=content,
                encoding=encoding or "utf-8",
                language=detect_language(path),
            )
            create_document("filedocument", doc)
            saved += 1

    # Update simple count
    create_document("repo_sync_log", {"full_name": full_name, "saved": saved})

    return {"status": "ok", "saved": saved, "repo": full_name}


@app.get("/api/files")
def list_files(repo: str = Query(..., description="owner/name"), limit: int = 100):
    items = get_documents("filedocument", {"repo_full_name": repo}, limit=limit)
    # Convert ObjectIds and datetime to strings
    for it in items:
        it["_id"] = str(it["_id"]) if "_id" in it else None
        for k in ("created_at", "updated_at"):
            if k in it:
                it[k] = str(it[k])
    return {"items": items}


@app.get("/api/repos")
def list_repos(limit: int = 50):
    items = get_documents("repo", {}, limit=limit)
    for it in items:
        it["_id"] = str(it["_id"]) if "_id" in it else None
        for k in ("created_at", "updated_at"):
            if k in it:
                it[k] = str(it[k])
    return {"items": items}


if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
