"""
main.py — FastAPI application entry point for backend-python.

Start with:
    uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload

The app serves:
    GET  /health               — liveness probe
    POST /api/v1/jobs          — submit a research job
    GET  /api/v1/jobs/{id}/... — status, stream, result
"""

import logging

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.core.config import settings
from app.routers import jobs

# ── Logging ───────────────────────────────────────────────────────────────
logging.basicConfig(                 #Whenever something is logged, print it in this format.
    level=logging.INFO,              #2026-08-01 20:30:11 [INFO] app.main: Server started
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)

# ── App ───────────────────────────────────────────────────────────────────
app = FastAPI(                       #This creates the application object.... ~const app = express();
    title="Re-Search Python Service",
    description="LangGraph-powered research pipeline for the Re-Search platform.",
    version="0.1.0",
)

# ── CORS ──────────────────────────────────────────────────────────────────
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,     #Cookies and authorisation headers
    allow_methods=["*"], 
    allow_headers=["*"],
)

# ── Routes ────────────────────────────────────────────────────────────────
app.include_router(jobs.router)      #When the request starts with /api/v1/jobs, it will be handled by jobs.router
                                     #Adds job related routes to the application from jobs.py

@app.get("/health")
async def health():
    """Basic liveness check — used by docker-compose and Node gateway."""
    return {"status": "ok", "service": "backend-python"}


# ── Uvicorn runner (for `python -m app.main`) ─────────────────────────────
if __name__ == "__main__":           #"Run the code below only if this file is executed directly."
    import uvicorn                   # Uvicorn is a lightweight ASGI server.

    uvicorn.run(
        "app.main:app",
        host="0.0.0.0",
        port=settings.python_service_port,
        reload=True,
    )
