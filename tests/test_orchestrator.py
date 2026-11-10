"""Tests for the self-healing ClusterController."""

from __future__ import annotations

import asyncio

import pytest

from src.orchestrator import (
    ClusterController,
    ControllerConfig,
    DatabaseNode,
    NodeHealth,
    NodeRole,
    build_simulated_cluster,
)


class FakeNode(DatabaseNode):
    """Deterministic node whose health and lag are set by the test."""

    def __init__(self, node_id: str, role: NodeRole, lag: float = 0.0) -> None:
        super().__init__(node_id, role)
        self.healthy = True
        self.lag = lag
        self.restarts = 0
        self.fail_promotion = False

    async def health_check(self) -> bool:
        return self.healthy

    async def replication_lag(self) -> float:
        return 0.0 if self.role is NodeRole.PRIMARY else self.lag

    async def promote(self) -> None:
        if self.fail_promotion:
            raise RuntimeError("promotion refused")
        self._role = NodeRole.PRIMARY

    async def demote(self) -> None:
        self._role = NodeRole.REPLICA

    async def restart(self) -> None:
        self.restarts += 1
        self.healthy = True


def make_cluster(threshold: int = 2) -> tuple[ClusterController, FakeNode, FakeNode, FakeNode]:
    primary = FakeNode("db-0", NodeRole.PRIMARY)
    fast = FakeNode("db-1", NodeRole.REPLICA, lag=0.1)
    slow = FakeNode("db-2", NodeRole.REPLICA, lag=1.5)
    controller = ClusterController(
        [primary, fast, slow], ControllerConfig(interval=0.01, failure_threshold=threshold)
    )
    return controller, primary, fast, slow


def test_requires_exactly_one_primary() -> None:
    with pytest.raises(ValueError, match="exactly one primary"):
        ClusterController([FakeNode("a", NodeRole.REPLICA)])
    with pytest.raises(ValueError, match="duplicate"):
        ClusterController([FakeNode("a", NodeRole.PRIMARY), FakeNode("a", NodeRole.REPLICA)])


def test_suspect_before_threshold() -> None:
    controller, primary, *_ = make_cluster(threshold=3)
    primary.healthy = False
    asyncio.run(controller.reconcile_once())
    assert controller.statuses[0].health is NodeHealth.SUSPECT
    assert controller.primary is primary


def test_failover_promotes_least_lagged_replica_and_heals_old_primary() -> None:
    controller, primary, fast, slow = make_cluster(threshold=2)
    primary.healthy = False

    asyncio.run(controller.run(max_cycles=2))

    assert controller.primary is fast
    assert primary.role is NodeRole.REPLICA
    assert primary.restarts == 1
    assert slow.role is NodeRole.REPLICA
    assert controller.failovers == 1


def test_failover_skips_replica_that_refuses_promotion() -> None:
    controller, primary, fast, slow = make_cluster(threshold=1)
    primary.healthy = False
    fast.fail_promotion = True

    asyncio.run(controller.reconcile_once())
    assert controller.primary is slow


def test_no_failover_without_healthy_replica() -> None:
    controller, primary, fast, slow = make_cluster(threshold=1)
    primary.healthy = fast.healthy = slow.healthy = False

    asyncio.run(controller.reconcile_once())
    assert controller.failovers == 0
    assert primary.role is NodeRole.PRIMARY


def test_node_quarantined_after_max_restarts() -> None:
    node = FakeNode("db-1", NodeRole.REPLICA)
    controller = ClusterController(
        [FakeNode("db-0", NodeRole.PRIMARY), node],
        ControllerConfig(interval=0.01, failure_threshold=1, max_restarts=1),
    )

    async def scenario() -> None:
        node.healthy = False
        await controller.reconcile_once()  # restart #1
        node.healthy = False
        await controller.reconcile_once()  # limit reached -> quarantine

    asyncio.run(scenario())
    assert node.restarts == 1
    assert controller.statuses[1].health is NodeHealth.QUARANTINED


def test_stop_event_halts_loop() -> None:
    controller, *_ = make_cluster()

    async def scenario() -> None:
        stop = asyncio.Event()
        task = asyncio.create_task(controller.run(stop_event=stop))
        await asyncio.sleep(0.05)
        stop.set()
        await asyncio.wait_for(task, timeout=1)

    asyncio.run(scenario())
    assert controller.cycles >= 1


def test_simulated_cluster_is_reproducible() -> None:
    def run(seed: int) -> list[dict[str, object]]:
        controller = ClusterController(
            build_simulated_cluster(4, failure_rate=0.4, seed=seed),
            ControllerConfig(interval=0.001, failure_threshold=1),
        )
        asyncio.run(controller.run(max_cycles=10))
        return controller.snapshot()

    assert run(42) == run(42)
