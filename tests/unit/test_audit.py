from backupsys.security.audit import AuditLog


def test_chain_ok_and_tamper_detected() -> None:
    a = AuditLog()
    for i in range(5):
        a.log("evento", n=i)
    assert a.verify().ok and a.verify().entries == 5
    lines = a.path.read_text().splitlines()
    lines[2] = lines[2].replace('"n": 2', '"n": 99')
    a.path.write_text("\n".join(lines) + "\n")
    v = a.verify()
    assert not v.ok and v.broken_at == 3


def test_removed_line_detected() -> None:
    a = AuditLog()
    for i in range(4):
        a.log("evento", n=i)
    lines = a.path.read_text().splitlines()
    del lines[1]
    a.path.write_text("\n".join(lines) + "\n")
    assert not a.verify().ok


def test_entries_newest_first() -> None:
    a = AuditLog()
    a.log("primo")
    a.log("secondo")
    assert [e["action"] for e in a.entries()] == ["secondo", "primo"]
