"""A managed connector client that mints links and flips its accounts active on demand."""

from __future__ import annotations

import threading


class FakeManagedClient:
    def __init__(self):
        self.connected = threading.Event()
        self.mints = 0

    def connections(self, names, *, reinitiate=False, **_):
        self.mints += 1
        return {"results": [
            {"connector": name, "status": "initiated", "connect_url": f"https://connect.example/{name}",
             "connection_id": f"ca_{name}"}
            for name in names
        ]}

    def account_status(self, connection_id, *, timeout=None):
        name = connection_id.removeprefix("ca_")
        return {"connector": name, "connectionId": connection_id, "status": "active" if self.connected.is_set() else "pending"}
