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
        self.assertAlmostEqual(self.run_job(age=600, anon_peak=0.05, mem_now=0.05), 0.45)

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
        with mock.patch.dict(os.environ, base or {}, clear=True), mock.patch.object(os, "cpu_count", return_value=20), \
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
