"""nomad 1.14.2: a workstation's own disks raise alerts at the disk thresholds.

The workstation collector sized every mount since 1.7.24, and the Console
showed a machine's local disks, but nothing alerted when one filled: on one
lab machine a local /home stood at 97%.
"""
import pytest

from nomad.alerts import thresholds as th

GB = 10 ** 9


def mount(path, fstype, source, total_gb, used_gb):
    return {"mountpoint": path, "fstype": fstype, "source": source, "is_mounted": 1,
            "is_responsive": 1, "total_bytes": total_gb * GB, "used_bytes": used_gb * GB,
            "avail_bytes": (total_gb - used_gb) * GB}


def reading(hostname="ws07", status="online", mounts=None):
    return {"hostname": hostname, "status": status, "disk_usage_pct": 41.0,
            "mount_snapshots": mounts if mounts is not None else [
                mount("/", "btrfs", "/dev/nvme0n1p2", 1000, 412),
                mount("/home", "btrfs", "/dev/nvme0n1p2", 1000, 412),     # the same disk
                mount("/data", "xfs", "/dev/sda1", 4000, 3520),            # 88%
                mount("/localscratch", "ext4", "/dev/sdb1", 2000, 1950),   # 97.5%
                mount("/mnt/lab", "nfs4", "nas:/export/lab", 50000, 49900),
                mount("/media/someone/USB", "vfat", "/dev/sdc1", 64, 63),
                mount("/home/x/remote", "fuse.sshfs", "host:/x", 500, 499),
                mount("/win", "fuseblk", "/dev/sdd1", 1000, 990),          # NTFS: a local disk
            ]}


@pytest.fixture
def sent(monkeypatch):
    out = []
    monkeypatch.setattr(th, "send_alert", lambda **kw: out.append(kw))
    monkeypatch.setattr(th, "get_dispatcher", lambda: object())
    return out


def test_the_local_disks_of_a_reading():
    disks = th.workstation_disks(reading())
    assert [(d["path"], d["also"]) for d in disks] == [
        ("/", ["/home"]), ("/win", []), ("/data", []), ("/localscratch", [])]
    assert disks[2]["used_percent"] == 88.0


def test_full_disks_alert_about_the_workstation(sent):
    th.ThresholdChecker({}).check("workstation", [reading()], host="hub01")
    got = sorted((a["host"], a["subject"], a["severity"], a["source"]) for a in sent)
    assert got == [("ws07", "/data", "warning", "workstation_disk"),
                   ("ws07", "/localscratch", "critical", "workstation_disk"),
                   ("ws07", "/win", "critical", "workstation_disk")]
    msg = next(a["message"] for a in sent if a["subject"] == "/data")
    assert msg == "Local disk /data at 88.0% (480.0 GB free; threshold: 80%)"


def test_the_system_disk_says_so_and_names_its_other_mounts(sent):
    r = reading(mounts=[mount("/", "xfs", "/dev/sda2", 500, 480),
                        mount("/home", "xfs", "/dev/sda2", 500, 480)])
    th.ThresholdChecker({}).check("workstation", [r], host="hub01")
    (a,) = sent
    assert a["subject"] == "/" and "the system disk" in a["message"]
    assert "(also /home)" in a["message"]


def test_the_sites_thresholds_hold(sent):
    cfg = {"alerts": {"thresholds": {"disk": {"used_percent_warning": 90,
                                              "used_percent_critical": 99}}}}
    th.ThresholdChecker(cfg).check("workstation", [reading()], host="hub01")
    assert sorted((a["subject"], a["severity"]) for a in sent) == [
        ("/localscratch", "warning"), ("/win", "critical")]


def test_an_offline_machine_or_one_without_sizes_raises_nothing(sent):
    th.ThresholdChecker({}).check("workstation", [
        reading(status="offline"),
        {"hostname": "ws08", "status": "offline"},
        reading(hostname="ws09", mounts=[{**mount("/data", "xfs", "/dev/sda1", 100, 99),
                                          "total_bytes": None}]),
    ], host="hub01")
    assert sent == []


def test_localhost_is_named_as_the_collecting_host(sent):
    th.ThresholdChecker({}).check("workstation", [reading(hostname="localhost")],
                                  host="labserver")
    assert {a["host"] for a in sent} == {"labserver"}


def test_each_disk_is_its_own_condition():
    from nomad.alerts.dispatcher import alert_key
    keys = {alert_key({"source": "workstation_disk", "host": "ws07", "subject": p,
                       "details": {"metric": "used_percent"}}) for p in ("/data", "/localscratch")}
    assert len(keys) == 2
