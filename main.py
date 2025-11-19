import os
import io
import re
import requests
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Optional, List

from database import create_document, db, get_documents
from schemas import Subscriber, Manuscript

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.get("/")
def read_root():
    return {"message": "Hello from FastAPI Backend!"}

@app.get("/api/hello")
def hello():
    return {"message": "Hello from the backend API!"}

@app.get("/test")
def test_database():
    """Test endpoint to check if database is available and accessible"""
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
            response["database_url"] = "✅ Configured"
            response["database_name"] = db.name if hasattr(db, 'name') else "✅ Connected"
            response["connection_status"] = "Connected"
            try:
                collections = db.list_collection_names()
                response["collections"] = collections[:10]
                response["database"] = "✅ Connected & Working"
            except Exception as e:
                response["database"] = f"⚠️  Connected but Error: {str(e)[:50]}"
        else:
            response["database"] = "⚠️  Available but not initialized"
            
    except Exception as e:
        response["database"] = f"❌ Error: {str(e)[:50]}"
    
    # Check environment variables
    response["database_url"] = "✅ Set" if os.getenv("DATABASE_URL") else "❌ Not Set"
    response["database_name"] = "✅ Set" if os.getenv("DATABASE_NAME") else "❌ Not Set"
    
    return response

@app.post("/api/subscribe")
def subscribe(subscriber: Subscriber):
    """Capture email signups for the book newsletter/sample chapter"""
    try:
        inserted_id = create_document("subscriber", subscriber)
        return {"status": "ok", "id": inserted_id}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

# -------------------- Manuscript ingestion --------------------
class IngestRequest(BaseModel):
    source_url: str
    format: Optional[str] = None  # pdf|docx|epub|md (auto-detected if not provided)
    cover_url: Optional[str] = None
    title: Optional[str] = None
    subtitle: Optional[str] = None


def _infer_format(url: str, hint: Optional[str]) -> str:
    if hint:
        return hint.lower()
    url_l = url.lower()
    if ".docx" in url_l:
        return "docx"
    if "/export?format=docx" in url_l:
        return "docx"
    if url_l.endswith(".pdf") or "format=pdf" in url_l:
        return "pdf"
    if url_l.endswith(".epub"):
        return "epub"
    if url_l.endswith(".md") or "raw.githubusercontent.com" in url_l:
        return "md"
    return "docx"


def _extract_docx(file_bytes: bytes) -> str:
    from docx import Document
    doc = Document(io.BytesIO(file_bytes))
    paragraphs = []
    for p in doc.paragraphs:
        text = p.text.strip()
        if text:
            paragraphs.append(text)
    return "\n\n".join(paragraphs)


def _extract_pdf(file_bytes: bytes) -> str:
    from pypdf import PdfReader
    reader = PdfReader(io.BytesIO(file_bytes))
    texts = []
    for page in reader.pages[:50]:  # cap pages for safety
        try:
            texts.append(page.extract_text() or "")
        except Exception:
            continue
    return "\n\n".join(texts)


def _extract_epub(file_bytes: bytes) -> str:
    from ebooklib import epub
    from bs4 import BeautifulSoup
    book = epub.read_epub(io.BytesIO(file_bytes))
    texts = []
    for item in book.get_items():
        if item.get_type() == epub.ITEM_DOCUMENT:
            soup = BeautifulSoup(item.get_content(), 'html.parser')
            texts.append(soup.get_text(" "))
    return "\n\n".join(texts)


def _extract_md(file_bytes: bytes) -> str:
    return file_bytes.decode("utf-8", errors="ignore")


def _extract_toc_from_text(full_text: str) -> list:
    # Heuristic: lines in ALL CAPS or lines that look like headings / short lines
    lines = [l.strip() for l in full_text.splitlines() if l.strip()]
    toc = []
    for i, l in enumerate(lines[:800]):  # scan first chunk
        if len(l) <= 80 and (l.isupper() or re.match(r"^(chapter|part|section)\b", l.lower()) or len(l.split()) <= 6):
            # avoid obviously non-heading lines
            if len(l) >= 3 and not l.endswith(('.', '!', '?')):
                toc.append(l)
        if len(toc) >= 30:
            break
    # Deduplicate while preserving order
    seen = set()
    unique = []
    for h in toc:
        if h not in seen:
            unique.append(h)
            seen.add(h)
    return unique[:20]


def _make_sample(full_text: str) -> str:
    # Take first ~1200 words or up to first section break
    words = full_text.split()
    snippet = " ".join(words[:1200])
    return snippet.strip()

@app.post("/api/ingest")
def ingest_manuscript(req: IngestRequest):
    fmt = _infer_format(req.source_url, req.format)
    try:
        r = requests.get(req.source_url, timeout=30)
        if r.status_code != 200:
            raise HTTPException(status_code=400, detail=f"Failed to fetch document: HTTP {r.status_code}")
        content = r.content
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Error fetching document: {str(e)}")

    try:
        if fmt == "docx":
            full_text = _extract_docx(content)
        elif fmt == "pdf":
            full_text = _extract_pdf(content)
        elif fmt == "epub":
            full_text = _extract_epub(content)
        elif fmt == "md":
            full_text = _extract_md(content)
        else:
            raise HTTPException(status_code=400, detail=f"Unsupported format: {fmt}")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to parse {fmt}: {str(e)}")

    # Extract metadata
    toc = _extract_toc_from_text(full_text)
    sample_text = _make_sample(full_text)
    word_count = len(full_text.split())

    manuscript = Manuscript(
        source_url=req.source_url,
        format=fmt,
        title=req.title,
        subtitle=req.subtitle,
        cover_url=req.cover_url,
        toc=toc or None,
        sample_text=sample_text or None,
        word_count=word_count
    )

    try:
        doc_id = create_document("manuscript", manuscript)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to save manuscript: {str(e)}")

    return {"status": "ok", "id": doc_id, "format": fmt, "word_count": word_count, "toc": toc[:10]}

@app.get("/api/manuscript/latest")
def get_latest_manuscript():
    try:
        docs = get_documents("manuscript", {}, limit=1)
        if not docs:
            return {"exists": False}
        m = docs[-1]
        # sanitize ObjectId
        m.pop("_id", None)
        return {"exists": True, "manuscript": m}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/manuscript/sample")
def get_sample():
    try:
        docs = get_documents("manuscript", {}, limit=1)
        if not docs:
            raise HTTPException(status_code=404, detail="No manuscript available")
        m = docs[-1]
        return {"sample_text": m.get("sample_text"), "toc": m.get("toc")}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
