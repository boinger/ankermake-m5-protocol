from types import SimpleNamespace

import cli.pppp

from libflagship.pppp import Duid, PktPunchPkt


def _duid():
    return Duid(prefix="ABCDEF1", serial=123456, check="CHK01")


def test_probe_printer_ip_retries_before_succeeding(monkeypatch):
    connects = []

    class FakeApi:
        def __init__(self):
            self.sock = SimpleNamespace(close=lambda: None)

        def connect_lan_search(self):
            connects.append(True)

        def recv(self, timeout=None):
            if len(connects) == 1:
                raise TimeoutError
            return PktPunchPkt(duid=_duid())

    monkeypatch.setattr(
        "cli.pppp.AnkerPPPPAsyncApi.open_lan",
        lambda duid, host: FakeApi(),
    )

    printer = SimpleNamespace(p2p_duid=str(_duid()))

    assert cli.pppp.probe_printer_ip(printer, "10.0.0.25", timeout=0.8, attempts=2) is True
    assert len(connects) == 2


def test_lan_search_retries_and_deduplicates_replies(monkeypatch):
    persisted = []

    class FakeBroadcastApi:
        def __init__(self):
            self.sock = SimpleNamespace(close=lambda: None)
            self.addr = ("0.0.0.0", 32108)
            self.send_calls = 0
            self._responses = [
                (PktPunchPkt(duid=_duid()), "10.0.0.25"),
                TimeoutError(),
                (PktPunchPkt(duid=_duid()), "10.0.0.25"),
                TimeoutError(),
                TimeoutError(),
            ]

        def send(self, pkt):
            self.send_calls += 1

        def recv(self, timeout=None):
            item = self._responses.pop(0)
            if isinstance(item, tuple):
                msg, ip_addr = item
                self.addr = (ip_addr, 32108)
                return msg
            raise item

    fake_api = FakeBroadcastApi()

    monkeypatch.setattr("cli.pppp.pppp_open_broadcast", lambda dumpfile=None: fake_api)
    monkeypatch.setattr("cli.pppp.time.sleep", lambda seconds: None)
    monkeypatch.setattr(
        "cli.pppp.persist_printer_ip",
        lambda config, duid, ip_addr, printer_index=None: persisted.append((duid, ip_addr)) or True,
    )

    results = cli.pppp.lan_search(object(), timeout=0.9, retries=3)

    assert fake_api.send_calls == 3
    assert results == [
        {
            "duid": str(_duid()),
            "ip_addr": "10.0.0.25",
            "persisted": True,
        }
    ]
    assert persisted == [(str(_duid()), "10.0.0.25")]


def test_pppp_resolve_printer_ip_falls_back_to_saved_ip_when_probe_and_search_miss(monkeypatch):
    monkeypatch.setattr("cli.pppp.probe_printer_ip", lambda printer, ip_addr, timeout=2.0: False)
    monkeypatch.setattr("cli.pppp.lan_search", lambda config, timeout=2.0, dumpfile=None: [])

    printer = SimpleNamespace(p2p_duid=str(_duid()), ip_addr="10.0.0.25")

    resolved = cli.pppp.pppp_resolve_printer_ip(object(), printer, printer_index=0, timeout=0.5)

    assert resolved == "10.0.0.25"
