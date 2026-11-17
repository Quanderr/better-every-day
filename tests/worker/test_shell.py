import time

from bed.worker.shell import run


def test_runs_string_through_bash(tmp_path):
    r = run("echo hi && echo err >&2 && exit 3", tmp_path, {"PATH": "/usr/bin:/bin"}, 5)
    assert r.code == 3 and "hi" in r.output and "err" in r.output and not r.timed_out


def test_runs_list_command(tmp_path):
    r = run(["pwd"], tmp_path, {"PATH": "/usr/bin:/bin"}, 5)
    assert r.code == 0 and str(tmp_path.resolve()) in r.output


def test_timeout_kills_process_group(tmp_path):
    start = time.monotonic()
    r = run("sleep 30 & sleep 30", tmp_path, {"PATH": "/usr/bin:/bin"}, 1)
    assert r.timed_out and r.code != 0 and time.monotonic() - start < 10


def test_output_tail_limited(tmp_path):
    r = run("for i in $(seq 1 20000); do echo line$i; done", tmp_path, {"PATH": "/usr/bin:/bin"}, 20)
    assert len(r.output) == 20_000 and r.output.rstrip().endswith("line20000")


def test_env_is_exact(tmp_path):
    r = run("env", tmp_path, {"PATH": "/usr/bin:/bin", "ONLY": "1"}, 5)
    assert "ONLY=1" in r.output and "HOME=" not in r.output


def test_multibyte_split_across_chunks_not_garbled(tmp_path):
    r = run("{ printf a; yes ż | head -n 40000; } | tr -d '\\n'", tmp_path, {"PATH": "/usr/bin:/bin"}, 30)
    assert "�" not in r.output and r.output.endswith("ż")


def test_reap_strays_kills_other_processes_of_same_user():
    from bed.worker.shell import reap_strays
    killed = []
    procs = {1: 1000, 42: 1000, 43: 0, 44: 1000}
    reaped = []
    reap_strays(self_pid=1, uid=1000, list_procs=lambda: procs.items(), kill=killed.append,
                collect=lambda: reaped.append(True))
    assert sorted(killed) == [42, 44] and reaped == [True]


def test_reap_strays_disabled_outside_container():
    from bed.worker.shell import reap_strays
    killed = []
    reaped = []
    reap_strays(self_pid=5000, uid=1000, list_procs=lambda: {42: 1000}.items(), kill=killed.append,
                collect=lambda: reaped.append(True))
    assert killed == [] and reaped == []
