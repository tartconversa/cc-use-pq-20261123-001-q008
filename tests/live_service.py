from __future__ import annotations

from pathlib import Path
import socket
from tempfile import TemporaryDirectory
from threading import Thread
import time

import httpx
import uvicorn

from fieldproof.api import create_app
from tests.support import ApiCase, ROOT


def main():
    with TemporaryDirectory() as directory, socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        address, port = listener.getsockname()
        app = create_app(Path(directory) / "pilot.sqlite3")
        config = uvicorn.Config(app, host=address, port=port, log_level="error",
                                lifespan="off", timeout_graceful_shutdown=2)
        server = uvicorn.Server(config)
        thread = Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
        thread.start()
        try:
            deadline = time.monotonic() + 8
            while not server.started and thread.is_alive() and time.monotonic() < deadline:
                time.sleep(0.02)
            if not server.started:
                raise RuntimeError("FieldProof did not bind the loopback socket")
            with httpx.Client(base_url=f"http://{address}:{port}", timeout=4, trust_env=False) as client:
                scenario = ApiCase()
                scenario.client = client
                assert scenario.call("GET", "/health") == {"status": "ready"}
                state = scenario.setup_trial()
                cut = scenario.deliver("field-signals", scenario.green_events())["current_cut"]
                scenario.close(state["version"], cut)
                frozen = scenario.freeze(cut)
                evidence_id = frozen["evidence_id"]
                scenario.review(evidence_id, "field", "anna")
                scenario.review(evidence_id, "methods", "ben")
                bulletin = scenario.issue(evidence_id)
                assert bulletin["outcome"] == "advance"
                assert scenario.call("GET", ROOT + f"/evidence/{evidence_id}") == frozen
                print(f"Loopback service published simulated market bulletin {bulletin['edition']} "
                      f"from cut {cut}: {bulletin['outcome']}")
        finally:
            server.should_exit = True
            thread.join(5)
            if thread.is_alive():
                raise RuntimeError("FieldProof did not stop after the acceptance flow")


if __name__ == "__main__":
    main()
