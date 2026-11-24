"""Queue logic of jobs/job.py: reservations, learned peaks, caps, thread counts."""
import importlib.machinery
import importlib.util
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

os.environ["AGENTJOBS_DIR"] = tempfile.mkdtemp(prefix="agentjobs-test-")
_ld = importlib.machinery.SourceFileLoader("job", str(Path(__file__).resolve().parent.parent / "jobs" / "job.py"))
job = importlib.util.module_from_spec(importlib.util.spec_from_loader("job", _ld))
_ld.exec_module(job)
GB = job.GB


def rec(state="done", name="eval_2026_01", project="p", peak=1.0, cap=4.0, ran=100, why=None):
    return {"id": name, "project": project, "name": name, "state": state, "peak_bytes": int(peak * GB),
            "mem_gb": cap, "started": 1000, "ended": 1000 + ran, "why": why, "peak_gb": cap / 2}


class Reserved(unittest.TestCase):
    def run_job(self, age, anon_peak, mem_now, stated=6.0):
        m = {"peak_gb": stated, "started": 1000 - age, "anon_peak": int(anon_peak * GB)}
        with mock.patch.object(job, "now", return_value=1000), mock.patch.object(job, "job_mem", return_value=int(mem_now * GB)):
            return job.reserved(m) / GB

    def test_new_job_keeps_its_stated_peak(self):
        self.assertAlmostEqual(self.run_job(age=30, anon_peak=0.1, mem_now=0.1), 5.9)

    def test_old_small_job_frees_its_reservation(self):
        self.assertAlmostEqual(self.run_job(age=600, anon_peak=0.05, mem_now=0.05), 0.15)

    def test_old_job_keeps_headroom_over_its_real_peak(self):
        self.assertAlmostEqual(self.run_job(age=600, anon_peak=2.0, mem_now=1.0), 2.0)

    def test_reservation_is_never_more_than_stated(self):
        self.assertAlmostEqual(self.run_job(age=600, anon_peak=5.0, mem_now=5.5), 0.5)


class Measured(unittest.TestCase):
    """Linux rules: start on measured free memory, pause on measured pressure."""
    def patches(self, free=10.0, psi=0.0, paused=(), swap=20.0, mem=None):
        mem = mem or {}
        return [mock.patch.object(job, "free_now", return_value=free * GB), mock.patch.object(job, "mem_pressure", return_value=psi),
                mock.patch.object(job, "is_paused", side_effect=lambda m: m.get("id") in paused),
                mock.patch.object(job, "swap_free", return_value=swap * GB),
                mock.patch.object(job, "job_mem", side_effect=lambda m: mem.get(m["id"], 0) * GB)]

    def run_with(self, ps, fn):
        for p in ps:
            p.start()
        try:
            return fn()
        finally:
            for p in ps:
                p.stop()

    def test_door_uses_half_of_the_hint_and_at_least_start_gb(self):
        self.assertFalse(self.run_with(self.patches(free=5), lambda: job.door({"peak_given": 1, "peak_known": 6.6}, []))[0])  # known size wins
        self.assertTrue(self.run_with(self.patches(free=7), lambda: job.door({"peak_given": 40, "peak_known": 6.6}, []))[0])
        self.assertTrue(self.run_with(self.patches(free=2.5), lambda: job.door({"peak_given": 4}, []))[0])
        self.assertFalse(self.run_with(self.patches(free=2.5), lambda: job.door({"peak_given": 8}, []))[0])
        self.assertFalse(self.run_with(self.patches(free=1.5), lambda: job.door({}, []))[0])

    def test_door_is_closed_while_a_job_is_paused_or_pressure_is_high(self):
        self.assertFalse(self.run_with(self.patches(paused=("a",)), lambda: job.door({}, [{"id": "a", "state": "run"}]))[0])
        self.assertTrue(self.run_with(self.patches(paused=("a",)), lambda: job.door({"first": True}, [{"id": "a", "state": "run"}]))[0])
        self.assertFalse(self.run_with(self.patches(paused=("a",), free=1.5),
                                       lambda: job.door({"first": True}, [{"id": "a", "state": "run"}]))[0])
        waiting = [{"id": "d", "state": "queued", "first": True}]
        self.assertFalse(self.run_with(self.patches(), lambda: job.door({"id": "x"}, waiting))[0])
        self.assertTrue(self.run_with(self.patches(), lambda: job.door({"id": "d", "first": True}, waiting))[0])
        for avail, is_open in ((4, False), (20, True)):  # pressure closes the door only when the host is also low on memory
            with mock.patch.object(job, "available", return_value=avail * GB), mock.patch.object(job, "total_ram", return_value=64 * GB):
                self.assertEqual(self.run_with(self.patches(psi=9), lambda: job.door({}, []))[0], is_open)

    def test_better_host_only_when_this_host_has_no_core_and_another_has(self):
        import tempfile, pathlib, json, os
        d = pathlib.Path(tempfile.mkdtemp())
        peers = {"a": {"host": "me", "cores": 16, "load": 1, "room_gb": 20, "queued": 0, "paused": 0},
                 "b": {"host": "b", "cores": 16, "load": 6, "room_gb": 10, "queued": 0, "paused": 0},
                 "c": {"host": "c", "cores": 16, "load": 2, "room_gb": 1, "queued": 0, "paused": 0}, "d": None}
        (d / "peers.json").write_text(json.dumps(peers))
        with mock.patch.object(job, "STATE", d), mock.patch.object(job.platform, "node", return_value="me"), \
                mock.patch.object(job.os, "cpu_count", return_value=20):
            with mock.patch.object(job.os, "getloadavg", return_value=(80, 0, 0)):
                self.assertEqual(job.better_host(4)[0], "b")  # not itself, not the host with no room
                self.assertIsNone(job.better_host(40))  # no host has the room
                os.utime(d / "peers.json", (1, 1))
                self.assertIsNone(job.better_host(4))  # old file: start here
            (d / "peers.json").write_text(json.dumps(peers))
            with mock.patch.object(job.os, "getloadavg", return_value=(30, 0, 0)):
                self.assertIsNone(job.better_host(4))  # this host is fine

    def test_door_is_closed_when_swap_is_almost_full(self):
        for free, is_open in ((3, False), (20, True)):
            with mock.patch.object(job, "swap_total", return_value=55 * GB):
                self.assertEqual(self.run_with(self.patches(swap=free), lambda: job.door({}, []))[0], is_open)

    def test_victim_is_the_youngest_job_of_the_largest_project(self):
        t = job.now()
        jobs = [{"id": "old", "state": "run", "project": "big", "started": t - 9000}, {"id": "new", "state": "run", "project": "big", "started": t - 60},
                {"id": "other", "state": "run", "project": "small", "started": t - 5}, {"id": "tiny", "state": "run", "project": "big", "started": t - 1},
                {"id": "svc", "state": "run", "project": "big", "started": t - 2, "no_pause": True},
                {"id": "back", "state": "run", "project": "big", "started": t - 3, "resumed": t - 10},
                {"id": "daily", "state": "run", "project": "big", "started": t - 4, "first": True}]
        mem = {"old": 8, "new": 4, "other": 6, "tiny": 0.1, "svc": 9, "back": 5, "daily": 3}
        order = self.run_with(self.patches(mem=mem), lambda: [m["id"] for m in job.victims(jobs)])
        self.assertEqual(order, ["new", "old", "other", "back", "daily"])

    def test_relieve_pauses_and_kills_only_when_swap_is_full(self):
        t = job.now()
        jobs = [{"id": "a", "state": "run", "project": "p", "started": t - 60, "unit": "u", "slice": "s"},
                {"id": "b", "state": "run", "project": "p", "started": t - 600, "unit": "u2", "slice": "s"}]
        for swap, want in ((20, "pause"), (2.5, "kill")):
            with mock.patch.object(job, "cg_write", return_value=True) as w, mock.patch.object(job, "kill_job") as k, \
                    mock.patch.object(job, "update"), mock.patch.object(job, "watch_log"), mock.patch.object(job, "Lock"):
                got = self.run_with(self.patches(swap=swap, mem={"a": 3, "b": 3}), lambda: job.relieve(jobs))
            self.assertEqual(got, want)
            self.assertEqual(k.called, want == "kill")
            if want == "pause":
                self.assertTrue(str(w.call_args_list[0][0][0]).endswith("u.service/cgroup.freeze"))

    def test_relieve_kills_a_lone_job_only_if_it_is_large(self):
        t = job.now()
        one = {"id": "a", "state": "run", "project": "p", "started": t - 60, "unit": "u", "slice": "s", "pauses": 2}
        frozen = {"id": "b", "state": "run", "project": "p", "started": t - 600, "unit": "u2", "slice": "s"}
        for jobs, paused, gb, want in (([one], (), 1, "pause"), ([one], (), 20, "kill"), ([one, frozen], ("b",), 20, "pause")):
            with mock.patch.object(job, "cg_write", return_value=True), mock.patch.object(job, "kill_job") as k, \
                    mock.patch.object(job, "update"), mock.patch.object(job, "watch_log"), mock.patch.object(job, "Lock"), \
                    mock.patch.object(job, "total_ram", return_value=32 * GB):
                got = self.run_with(self.patches(paused=paused, mem={"a": gb, "b": 3}), lambda: job.relieve(jobs))
            self.assertEqual((got, k.called), (want, want == "kill"))

    def test_relieve_kills_a_job_due_for_its_twelfth_pause(self):
        t = job.now()
        for pauses, want in ((10, "pause"), (11, "kill")):
            jobs = [{"id": "a", "state": "run", "project": "p", "started": t - 60, "unit": "u", "slice": "s",
                     "pauses": pauses},
                    {"id": "b", "state": "run", "project": "q", "started": t - 600, "unit": "u2", "slice": "s"}]
            with mock.patch.object(job, "cg_write", return_value=True), mock.patch.object(job, "kill_job") as k, \
                    mock.patch.object(job, "update"), mock.patch.object(job, "watch_log"), mock.patch.object(job, "Lock"):
                got = self.run_with(self.patches(mem={"a": 3, "b": 0.1}), lambda: job.relieve(jobs))
            self.assertEqual((got, k.called), (want, want == "kill"))



class Resume(unittest.TestCase):
    def go(self, waited, avail, swapped=5.3, others=True):
        """resume_one for a job paused 'waited' s ago with 'swapped' GB in swap. Returns 'resume', 'kill' or 'wait'."""
        j = {"id": "p", "state": "run", "unit": "u", "slice": "s", "paused": 10000 - waited, "started": 1}
        jobs = [j] + ([{"id": "o", "state": "run", "unit": "v", "slice": "s", "started": 2}] if others else [])
        with mock.patch.object(job, "now", return_value=10000), mock.patch.object(job, "is_paused", side_effect=lambda m: m["id"] == "p"),                 mock.patch.object(job, "cg_int", return_value=int(swapped * GB)), mock.patch.object(job, "available", return_value=int(avail * GB)),                 mock.patch.object(job, "margin", return_value=3 * GB), mock.patch.object(job, "cg_write") as w,                 mock.patch.object(job, "kill_job") as k, mock.patch.object(job, "update"), mock.patch.object(job, "watch_log"),                 mock.patch.object(job, "event"):
            got = job.resume_one(jobs)
        return "kill" if k.called else "resume" if got and w.called else "wait"

    def test_margin_is_kept_at_first_and_dropped_after_a_long_pause(self):
        self.assertEqual(self.go(600, 6.5), "wait")  # needs 5.3 + 3 + 2
        self.assertEqual(self.go(600, 10.5), "resume")
        self.assertEqual(self.go(job.STUCK_S + 1, 6.5), "resume")  # alphas-1, Nov 24: was paused 2 h beside small jobs

    def test_a_job_that_can_never_fit_is_killed_late_when_other_jobs_run(self):
        self.assertEqual(self.go(job.STUCK_S + 1, 4), "wait")
        self.assertEqual(self.go(4 * job.STUCK_S + 1, 4), "kill")
        self.assertEqual(self.go(job.STUCK_S + 1, 4, others=False), "kill")
        self.assertEqual(self.go(60, 5.5, others=False), "resume")

    def test_a_younger_job_that_fits_resumes_before_an_older_one_that_does_not(self):
        """alphas-1, Nov 24: 6.3 GB and 2.1 GB jobs paused 26-34 min beside small jobs, 3.6 GB available."""
        def go(waited):
            jobs = [{"id": "big", "state": "run", "unit": "u1", "slice": "s", "paused": 10000 - waited, "started": 1},
                    {"id": "small", "state": "run", "unit": "u2", "slice": "s", "paused": 10000 - waited, "started": 2},
                    {"id": "o", "state": "run", "unit": "u3", "slice": "s", "started": 3}]
            swap = {"big": 5.0, "small": 1.8, "o": 0}
            with mock.patch.object(job, "now", return_value=10000),                     mock.patch.object(job, "is_paused", side_effect=lambda m: m["id"] in ("big", "small")),                     mock.patch.object(job, "unit_cgroup", side_effect=lambda m: Path("/cg") / m["id"]),                     mock.patch.object(job, "cg_int", side_effect=lambda p: int(swap[p.parent.name] * GB)),                     mock.patch.object(job, "available", return_value=int(3.6 * GB)),                     mock.patch.object(job, "margin", return_value=3 * GB), mock.patch.object(job, "cg_write") as w,                     mock.patch.object(job, "kill_job") as k, mock.patch.object(job, "update") as u,                     mock.patch.object(job, "watch_log"), mock.patch.object(job, "event"):
                job.resume_one(jobs)
            return [c.args[0] for c in u.call_args_list], k.called, w.called
        self.assertEqual(go(600), ([], False, False))  # both need the margin
        self.assertEqual(go(job.STUCK_S + 1), (["small"], False, True))
        self.assertEqual(go(4 * job.STUCK_S + 1)[0], ["small"])  # the one that fits resumes; nothing is killed


class Plan(unittest.TestCase):
    def test_room_holds_the_largest_regrowth_and_a_share_of_the_others(self):
        t = job.now()
        jobs = [{"state": "run", "started": t - 3600, "r": 12}, {"state": "run", "started": t - 3600, "r": 8},
                {"state": "run", "started": t - 10, "r": 4}, {"state": "queued", "r": 99}]
        with mock.patch.object(job, "available", return_value=40 * GB), mock.patch.object(job, "margin", return_value=5 * GB), \
                mock.patch.object(job, "mem_pressure", return_value=0), mock.patch.object(job, "cg_int", return_value=None), \
                mock.patch.object(job, "reserved", side_effect=lambda m: m["r"] * GB):
            self.assertAlmostEqual(job.room(jobs) / GB, 40 - 5 - 4 - 12 - 0.25 * 8)

    def test_no_history_uses_the_guess_and_a_loose_cap(self):
        self.assertEqual(job.plan("p", "new_job", 4.0, None, jobs=[]), (4.0, 12.0, ""))

    def test_no_guess_no_history(self):
        self.assertEqual(job.plan("p", "new_job", None, None, jobs=[])[:2], (2.0, 6.0))

    def test_history_wins_over_a_guess_that_is_too_high(self):
        peak, mem, note = job.plan("p", "eval_2026_07", 4.0, None, jobs=[rec(peak=0.4), rec(peak=0.3)])
        self.assertEqual(peak, 0.44)
        self.assertEqual(mem, 6.0)  # still 1.5x the caller's guess: this run may be bigger
        self.assertIn("2 earlier runs", note)

    def test_a_run_killed_at_its_cap_raises_the_next_peak(self):
        peak, mem, _ = job.plan("p", "eval_2026_07", 4.0, None, jobs=[rec(state="killed", cap=6.0, why="over its memory cap")])
        self.assertEqual(peak, 9.0)
        self.assertGreaterEqual(mem, peak * 1.5)

    def test_a_good_run_after_a_kill_replaces_the_kill(self):
        jobs = [rec(peak=1.0), rec(state="killed", cap=6.0, why="over its memory cap"), rec(peak=6.4)]
        self.assertEqual(job.plan("p", "eval_2026_07", 4.0, None, jobs=jobs)[0], 7.04)

    def test_other_projects_names_and_short_runs_do_not_count(self):
        jobs = [rec(project="q", peak=9), rec(name="train_2026_01", peak=9), rec(peak=9, ran=3), rec(state="failed", peak=9)]
        self.assertEqual(job.plan("p", "eval_2026_07", 1.0, None, jobs=jobs), (1.0, 3.0, ""))

    def test_explicit_cap_is_kept_but_never_below_the_learned_peak(self):
        self.assertEqual(job.plan("p", "eval_2026_07", 1.0, 3.0, jobs=[rec(peak=4.0)])[:2], (4.4, 6.6))
        self.assertEqual(job.plan("p", "new", 1.0, 3.0, jobs=[])[:2], (1.0, 3.0))


class Threads(unittest.TestCase):
    def env(self, load, cpus=None, base=None):
        with mock.patch.object(job, "SYS", "Linux"), mock.patch.dict(os.environ, base or {}, clear=True), mock.patch.object(os, "cpu_count", return_value=20), \
                mock.patch.object(os, "getloadavg", return_value=(load, 0, 0)), mock.patch("builtins.print"):
            return job.thread_env({"cpus": cpus})

    def test_idle_host_gives_many_threads_busy_host_gives_few(self):
        self.assertEqual(self.env(load=1)["OMP_NUM_THREADS"], "16")
        self.assertEqual(self.env(load=45)["OMP_NUM_THREADS"], "4")

    def test_the_callers_own_value_is_kept(self):
        e = self.env(load=45, base={"OMP_NUM_THREADS": "2"})
        self.assertEqual(e["OMP_NUM_THREADS"], "2")
        self.assertEqual(e["MKL_NUM_THREADS"], "4")

    def test_a_value_from_an_outer_job_is_not_the_callers_choice(self):
        e = self.env(load=1, base={"OMP_NUM_THREADS": "4", "JOB_THREADS_AUTO": "4"})
        self.assertEqual(e["OMP_NUM_THREADS"], "16")

    def test_the_callers_value_wins_over_a_host_default(self):
        e = self.env(load=1, base={"OMP_NUM_THREADS": "1", "MKL_NUM_THREADS": "4", "JOB_THREADS_AUTO": "4"})
        self.assertEqual((e["OMP_NUM_THREADS"], e["MKL_NUM_THREADS"]), ("1", "16"))

    def test_cpus_flag_wins(self):
        self.assertEqual(self.env(load=1, cpus=3, base={"OMP_NUM_THREADS": "9"})["OMP_NUM_THREADS"], "3")


if __name__ == "__main__":
    unittest.main()


class HeldBack(unittest.TestCase):
    """Room that a younger job must leave for an older waiter."""

    def held(self, me_peak, waiters, r=2.0, running_gb=10.0):
        me = {"id": "me", "peak_gb": me_peak, "created": 9000, "state": "queued"}
        jobs = [me, {"id": "r", "state": "run", "peak_gb": running_gb}]
        jobs += [{"id": "w%d" % i, "state": "queued", "peak_gb": p, "created": c} for i, (p, c) in enumerate(waiters)]
        with mock.patch.object(job, "now", return_value=10000), mock.patch.object(job, "job_mem", return_value=0), \
                mock.patch.object(job, "reserved", return_value=int(running_gb * GB)):
            return job.held_back(me, jobs, r * GB) / GB

    def test_only_the_oldest_starved_waiter_holds_room(self):
        self.assertAlmostEqual(self.held(3.0, [(6.0, 100), (8.0, 200)]), 6.0)

    def test_a_waiter_that_cannot_fit_on_an_empty_host_holds_nothing(self):
        self.assertAlmostEqual(self.held(3.0, [(20.0, 100)]), 0.0)

    def test_a_small_job_passes_the_hold(self):
        self.assertAlmostEqual(self.held(0.5, [(6.0, 100)]), 0.0)

    def test_a_recent_waiter_holds_nothing(self):
        self.assertAlmostEqual(self.held(3.0, [(6.0, 8000)]), 0.0)


class MacCPUAdmission(unittest.TestCase):
    def setUp(self):
        for patch in (mock.patch.object(job, "SYS", "Darwin"),
                      mock.patch.object(os, "cpu_count", return_value=14),
                      mock.patch.object(os, "getloadavg", return_value=(5, 5, 5))):
            patch.start()
            self.addCleanup(patch.stop)

    def test_cross_project_burst_reserves_ten_and_queues_six(self):
        jobs = []
        for i in range(16):
            me = {"id": str(i), "cpus": 1, "state": "queued", "project": str(i % 3)}
            if not job.mac_cpu_blocked(me, jobs):
                me["state"] = "run"
            jobs.append(me)
        self.assertEqual(sum(m["state"] == "run" for m in jobs), 10)
        self.assertEqual(sum(m["state"] == "queued" for m in jobs), 6)
        jobs[0]["state"] = "done"
        self.assertFalse(job.mac_cpu_blocked(jobs[-1], jobs))

    def test_declared_cpu_totals_and_old_jobs_count(self):
        self.assertTrue(job.mac_cpu_blocked({"cpus": 3}, [{"state": "run", "cpus": 8}]))
        self.assertTrue(job.mac_cpu_blocked({"cpus": 1}, [{"state": "run", "cpus": None}] * 3))
        self.assertFalse(job.mac_cpu_blocked({"cpus": 2}, [{"state": "run"}] * 2))

    def test_load_of_the_mac_is_not_a_gate(self):
        with mock.patch.object(os, "getloadavg", return_value=(60, 1, 1)):
            self.assertFalse(job.mac_cpu_blocked({"cpus": 1}, []))
        with mock.patch.object(os, "getloadavg", side_effect=OSError):
            self.assertFalse(job.mac_cpu_blocked({"cpus": 1}, []))

    def held(self, age, cores, cpus=4):
        m = {"state": "run", "cpus": cpus, "child_pgid": 1, "started": 1000 - age}
        with mock.patch.object(job, "now", return_value=1000), mock.patch.object(job, "mac_group_cpu", return_value=cores):
            return job.mac_cpu_reserved(m)

    def test_new_job_holds_its_declared_cpus(self):
        self.assertEqual(self.held(age=30, cores=0.0), 4)

    def test_old_idle_job_frees_its_slots(self):
        self.assertAlmostEqual(self.held(age=600, cores=0.0), 0.1)

    def test_old_busy_job_never_holds_more_than_declared(self):
        self.assertEqual(self.held(age=600, cores=6.0), 4)
        self.assertAlmostEqual(self.held(age=600, cores=1.0), 1.5)

    def test_idle_services_do_not_fill_the_slots(self):
        idle = [{"state": "run", "cpus": 1, "child_pgid": i + 1, "started": 1} for i in range(30)]
        with mock.patch.object(job, "now", return_value=1000), mock.patch.object(job, "mac_group_cpu", return_value=0.0):
            self.assertFalse(job.mac_cpu_blocked({"cpus": 4}, idle))

    def test_one_cpu_host_keeps_one_slot(self):
        with mock.patch.object(os, "cpu_count", return_value=1):
            self.assertEqual(job.mac_cpu_slots(), 1)

    def test_mac_threads_match_reservation_despite_inherited_values(self):
        with mock.patch.dict(os.environ, {"OMP_NUM_THREADS": "64", "MKL_NUM_THREADS": "64"}, clear=True):
            e = job.thread_env({})
            self.assertTrue(all(e[k] == "1" for k in job.THREAD_VARS))
            e = job.thread_env({"cpus": 3})
            self.assertTrue(all(e[k] == "3" for k in job.THREAD_VARS))

    def test_admit_checks_cpu_before_start_even_when_memory_fits(self):
        me = {"id": "me", "peak_gb": 0.1, "cpus": 1, "state": "queued", "created": 1}
        running = {"id": "other", "state": "run", "cpus": 10}
        with mock.patch.object(job, "Lock"), mock.patch.object(job, "all_jobs", side_effect=[
                [me, running], [dict(me, state="stopped"), running]]), \
                mock.patch.object(job, "room", return_value=100 * GB), \
                mock.patch.object(job, "held_back", return_value=0), \
                mock.patch.object(job.time, "sleep"), mock.patch.object(job, "update") as update:
            self.assertFalse(job.admit("me"))
            update.assert_not_called()

    def test_invalid_cpu_request_fails_before_creating_job(self):
        import argparse
        for cpus in (0, -1, 11):
            with self.subTest(cpus=cpus), self.assertRaises(SystemExit):
                job.cmd_run(argparse.Namespace(cmd=["true"], cpus=cpus))
class StartFailure(unittest.TestCase):
    def test_a_command_that_cannot_start_ends_failed(self):
        jid = "startfail"
        (job.STATE / jid).mkdir(parents=True, exist_ok=True)
        job.write({"id": jid, "state": "wait", "cmd": ["/no/such/program"], "cwd": "/", "peak_gb": 0.1, "mem_gb": 1,
                   "created": 0})
        with mock.patch.object(job, "admit", return_value=True), mock.patch.object(job, "SYS", "Darwin"):
            job.supervise(jid)
        m = job.read(jid)
        self.assertEqual((m["state"], m["exit"]), ("failed", 127))
        self.assertIn("/no/such/program", m["error"])


class StaleLock(unittest.TestCase):
    """A paused process that holds the admission lock must not stop the queue."""

    @unittest.skipIf(job.SYS == "Windows", "posix lock")
    def test_waiters_move_to_a_new_lock_when_the_holder_is_paused(self):
        import fcntl
        d = tempfile.mkdtemp(prefix="agentjobs-lock-")
        path = os.path.join(d, "x.lock")
        with mock.patch.object(job, "LOCK", path), mock.patch.object(job, "LOCK_STALE_S", 0.3):
            held = open(path, "a+")
            fcntl.flock(held, fcntl.LOCK_EX)  # the paused holder
            with mock.patch("builtins.print"):
                with job.Lock():
                    self.assertEqual(job.lock_gen(), 1)
            with job.Lock():  # later waiters use the new file at once
                pass
            held.close()

    @unittest.skipIf(job.SYS == "Windows", "posix lock")
    def test_free_lock_keeps_its_file(self):
        d = tempfile.mkdtemp(prefix="agentjobs-lock-")
        with mock.patch.object(job, "LOCK", os.path.join(d, "x.lock")):
            with job.Lock():
                self.assertEqual(job.lock_gen(), 0)
