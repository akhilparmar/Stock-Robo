"""Run the OI-spurts paper tester by itself, on port 8002."""
from __future__ import annotations

import logging


def main() -> None:
    import uvicorn

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    uvicorn.run("oi_spurts.app:app", host="0.0.0.0", port=8002, reload=False)


if __name__ == "__main__":
    main()
