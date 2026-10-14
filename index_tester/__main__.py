"""Run the Nifty tester by itself, on port 8001."""
from __future__ import annotations


def main() -> None:
    import uvicorn

    uvicorn.run("index_tester.app:app", host="127.0.0.1", port=8001, reload=False)


if __name__ == "__main__":
    main()
