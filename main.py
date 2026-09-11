"""Convenience entry point so `python main.py` works.

The app itself lives in `app/main.py`; this is a shim for the common reflex of
running a root `main.py`. Both are equivalent:

    python main.py
    python -m app.main
    uvicorn app.main:app --host 0.0.0.0 --port 8080    # what the container runs
"""

from app.main import app  # noqa: F401  (re-exported so `uvicorn main:app` works too)

if __name__ == "__main__":
    import uvicorn

    from app.settings import get_settings

    settings = get_settings()
    uvicorn.run(app, host=settings.host, port=settings.port)
