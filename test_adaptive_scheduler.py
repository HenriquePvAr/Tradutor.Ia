import unittest

from adaptive_scheduler import (
    AdaptiveResourceScheduler,
    ResourceSnapshot,
    SchedulerConfig,
    classify_memory_pressure,
    safe_memory_budget_mb,
)


class FakeClock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


def snapshot(
    available=16000,
    total=32000,
    ram=45,
    cpu=40,
    physical=8,
):
    return ResourceSnapshot(
        available_memory_mb=available,
        total_memory_mb=total,
        ram_percent=ram,
        cpu_percent=cpu,
        swap_used_mb=0,
        physical_cores=physical,
        logical_cores=physical * 2,
    )


class AdaptiveSchedulerTests(unittest.TestCase):
    def test_pressure_states(self):
        self.assertEqual(classify_memory_pressure(40), "NORMAL")
        self.assertEqual(classify_memory_pressure(73), "ELEVATED")
        self.assertEqual(classify_memory_pressure(83), "HIGH")
        self.assertEqual(classify_memory_pressure(91), "CRITICAL")

    def test_safe_budget_preserves_system_reserve_and_margin(self):
        budget = safe_memory_budget_mb(
            available_memory_mb=12000,
            total_memory_mb=32000,
            margin_percent=20,
            min_system_reserve_gb=4,
            pipeline_reserve_gb=1,
        )
        self.assertEqual(budget, 12000 - 6400 - 1024)

    def test_scale_up_is_gradual_and_uses_cooldown(self):
        clock = FakeClock()
        scheduler = AdaptiveResourceScheduler(
            SchedulerConfig(
                min_workers=1,
                max_workers=4,
                worker_estimated_peak_mb=1000,
                scale_up_cooldown_seconds=10,
            ),
            now_func=clock,
        )
        first = scheduler.decide(snapshot(), current_workers=1, pending_jobs=10)
        self.assertEqual(first.target_workers, 2)
        self.assertEqual(first.reason, "scale_up_gradual")
        second = scheduler.decide(snapshot(), current_workers=2, pending_jobs=10)
        self.assertEqual(second.target_workers, 2)
        self.assertEqual(second.reason, "scale_up_cooldown")
        clock.advance(11)
        third = scheduler.decide(snapshot(), current_workers=2, pending_jobs=10)
        self.assertEqual(third.target_workers, 3)

    def test_high_pressure_blocks_growth(self):
        scheduler = AdaptiveResourceScheduler(
            SchedulerConfig(min_workers=1, max_workers=4, worker_estimated_peak_mb=1000)
        )
        decision = scheduler.decide(
            snapshot(ram=84, cpu=35),
            current_workers=2,
            pending_jobs=20,
        )
        self.assertEqual(decision.pressure, "HIGH")
        self.assertLessEqual(decision.target_workers, 2)

    def test_critical_pressure_returns_to_min_worker(self):
        scheduler = AdaptiveResourceScheduler(
            SchedulerConfig(min_workers=1, max_workers=4, worker_estimated_peak_mb=1000)
        )
        decision = scheduler.decide(
            snapshot(ram=93, cpu=70),
            current_workers=4,
            pending_jobs=20,
        )
        self.assertEqual(decision.pressure, "CRITICAL")
        self.assertEqual(decision.target_workers, 1)

    def test_cpu_saturation_prevents_growth(self):
        scheduler = AdaptiveResourceScheduler(
            SchedulerConfig(min_workers=1, max_workers=4, worker_estimated_peak_mb=1000)
        )
        decision = scheduler.decide(
            snapshot(ram=45, cpu=96),
            current_workers=2,
            pending_jobs=20,
        )
        self.assertEqual(decision.reason, "hold_high_pressure")
        self.assertEqual(decision.target_workers, 2)

    def test_expensive_worker_limits_capacity(self):
        scheduler = AdaptiveResourceScheduler(
            SchedulerConfig(
                min_workers=1,
                max_workers=4,
                worker_estimated_peak_mb=6000,
                scale_up_cooldown_seconds=0,
            )
        )
        decision = scheduler.decide(
            snapshot(available=9000, total=32000, ram=55, cpu=40),
            current_workers=2,
            pending_jobs=20,
        )
        self.assertEqual(decision.target_workers, 1)
        self.assertIn("scale_down", decision.reason)

    def test_worker_observation_updates_peak_conservatively(self):
        scheduler = AdaptiveResourceScheduler(
            SchedulerConfig(worker_estimated_peak_mb=1000)
        )
        scheduler.update_worker_observation(2000)
        self.assertGreater(scheduler.estimated_worker_peak_mb, 1000)
        scheduler.update_worker_observation(500)
        self.assertGreater(scheduler.estimated_worker_peak_mb, 500)

    def test_no_psutil_path_can_be_represented_by_none_snapshot(self):
        # The real snapshot_from_psutil helper returns None on unsupported
        # platforms. Scheduler callers must then fall back conservatively.
        scheduler = AdaptiveResourceScheduler(SchedulerConfig(min_workers=1))
        decision = scheduler.decide(snapshot(), current_workers=1, pending_jobs=0)
        self.assertEqual(decision.target_workers, 1)


if __name__ == "__main__":
    unittest.main()
