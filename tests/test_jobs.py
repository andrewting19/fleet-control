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


class Plan(unittest.TestCase):
    def test_no_history_uses_the_guess_and_a_loose_cap(self):
        self.assertEqual(job.plan("p", "new_job", 4.0, None, jobs=[]), (4.0, 12.0, ""))

    def test_no_guess_no_history(self):
        self.assertEqual(job.plan("p", "new_job", None, None, jobs=[])[:2], (2.0, 6.0))

    def test_history_wins_over_a_guess_that_is_too_high(self):
        peak, mem, note = job.plan("p", "eval_2026_07", 4.0, None, jobs=[rec(peak=0.4), rec(peak=0.3)])
        self.assertEqual(peak, 0.5)
        self.assertEqual(mem, 6.0)  # still 1.5x the caller's guess: this run may be bigger
        self.assertIn("2 earlier runs", note)

    def test_a_run_killed_at_its_cap_raises_the_next_peak(self):
        peak, mem, _ = job.plan("p", "eval_2026_07", 4.0, None, jobs=[rec(state="killed", cap=6.0, why="over its memory cap")])
        self.assertEqual(peak, 9.0)
        self.assertGreaterEqual(mem, peak * 1.5)

    def test_a_good_run_after_a_kill_replaces_the_kill(self):
        jobs = [rec(peak=1.0), rec(state="killed", cap=6.0, why="over its memory cap"), rec(peak=6.4)]
        self.assertEqual(job.plan("p", "eval_2026_07", 4.0, None, jobs=jobs)[0], 8.0)

    def test_other_projects_names_and_short_runs_do_not_count(self):
        jobs = [rec(project="q", peak=9), rec(name="train_2026_01", peak=9), rec(peak=9, ran=3), rec(state="failed", peak=9)]
        self.assertEqual(job.plan("p", "eval_2026_07", 1.0, None, jobs=jobs), (1.0, 3.0, ""))

    def test_explicit_cap_is_kept_but_never_below_the_learned_peak(self):
        self.assertEqual(job.plan("p", "eval_2026_07", 1.0, 3.0, jobs=[rec(peak=4.0)])[:2], (5.0, 7.5))
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
