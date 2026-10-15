"""Run the loopback-only API: python -m rag.api."""
import sys

from rag.management.errors import public_error


def main():
    try:
        import uvicorn
        from .app import create_app
        from rag.ops.events import event, start_logging
        start_logging("api")
        event("api", "starting")
        # Keep search text and rejected inputs out of the HTTP access log.
        uvicorn.run(create_app(), host="127.0.0.1", port=8000, workers=1, access_log=False)
        return 0
    except Exception as exc:
        import json
        print(json.dumps(public_error(exc), ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
