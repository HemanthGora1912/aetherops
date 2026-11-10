"""Cluster control plane for AetherOps.

Defines the abstract :class:`DatabaseNode` interface, a deterministic
:class:`SimulatedDatabaseNode` for local clusters, and the asyncio-driven
:class:`ClusterController` that probes node health, performs automated
primary failover (choosing the least-lagged healthy replica), fences the old
primary and self-heals failed nodes.
"""

from __future__ import annotations

import asyncio
import logging
import math
import random
import time
from abc import ABC, abstractmethod
from collections.abc import Sequence
from dataclasses import dataclass, field
from enum import Enum

logger = logging.getLogger(__name__)


class NodeRole(str, Enum):
    PRIMARY = "primary"
    REPLICA = "replica"


class NodeHealth(str, Enum):
    HEALTHY = "healthy"
    SUSPECT = "suspect"
    UNHEALTHY = "unhealthy"
    RECOVERING = "recovering"
    QUARANTINED = "quarantined"


class ClusterError(Exception):
    """Base class for control-plane errors."""


class NodeOperationError(ClusterError):
    """A node rejected a lifecycle operation."""


# --------------------------------------------------------------------------- #
# Node interface
# --------------------------------------------------------------------------- #
class DatabaseNode(ABC):
    """Abstract database node managed by the controller."""

    def __init__(self, node_id: str, role: NodeRole) -> None:
        if not node_id:
            raise ValueError("node_id must be non-empty")
        self._node_id = node_id
        self._role = role

    @property
    def node_id(self) -> str:
        return self._node_id

    @property
    def role(self) -> NodeRole:
        return self._role

    @abstractmethod
    async def health_check(self) -> bool:
        """Return ``True`` if the node accepts connections and is consistent."""

    @abstractmethod
    async def replication_lag(self) -> float:
        """Seconds of replication lag behind the primary (0 for the primary)."""

    @abstractmethod
    async def promote(self) -> None:
        """Promote this replica to primary."""

    @abstractmethod
    async def demote(self) -> None:
        """Fence and demote this node to replica."""

    @abstractmethod
    async def restart(self) -> None:
        """Restart the node's database service."""

    def __repr__(self) -> str:
        return f"{type(self).__name__}(node_id={self.node_id!r}, role={self.role.value})"


class SimulatedDatabaseNode(DatabaseNode):
    """In-memory node that crashes randomly at a configurable rate."""

    def __init__(
        self,
        node_id: str,
        role: NodeRole,
        *,
        failure_rate: float = 0.05,
        rng: random.Random | None = None,
        latency: tuple[float, float] = (0.005, 0.02),
    ) -> None:
        super().__init__(node_id, role)
        if not 0.0 <= failure_rate <= 1.0:
            raise ValueError("failure_rate must be within [0, 1]")
        low, high = latency
        if not 0.0 <= low <= high:
            raise ValueError("latency must be a (low, high) pair with 0 <= low <= high")
        self.failure_rate = failure_rate
        self._rng = rng or random.Random()
        self._latency = latency
        self.alive = True

    async def _simulate_io(self) -> None:
        await asyncio.sleep(self._rng.uniform(*self._latency))

    async def health_check(self) -> bool:
        await self._simulate_io()
        if self.alive and self._rng.random() < self.failure_rate:
            self.alive = False
            logger.debug("%s crashed (simulated)", self.node_id)
        return self.alive

    async def replication_lag(self) -> float:
        await self._simulate_io()
        if not self.alive:
            return math.inf
        return 0.0 if self.role is NodeRole.PRIMARY else round(self._rng.uniform(0.0, 2.0), 3)

    async def promote(self) -> None:
        await self._simulate_io()
        if not self.alive:
            raise NodeOperationError(f"cannot promote {self.node_id}: node is down")
        self._role = NodeRole.PRIMARY

    async def demote(self) -> None:
        await self._simulate_io()
        self._role = NodeRole.REPLICA

    async def restart(self) -> None:
        await self._simulate_io()
        self.alive = True


def build_simulated_cluster(
    size: int = 3,
    *,
    failure_rate: float = 0.05,
    seed: int | None = None,
) -> list[DatabaseNode]:
    """Create ``size`` simulated nodes; the first is the primary."""
    if size < 1:
        raise ValueError("cluster size must be >= 1")
    master = random.Random(seed)
    return [
        SimulatedDatabaseNode(
            f"db-{index}",
            NodeRole.PRIMARY if index == 0 else NodeRole.REPLICA,
            failure_rate=failure_rate,
            rng=random.Random(master.getrandbits(64)),
        )
        for index in range(size)
    ]


# --------------------------------------------------------------------------- #
# Controller
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class ControllerConfig:
    interval: float = 5.0
    failure_threshold: int = 3
    health_timeout: float = 2.0
    max_restarts: int = 5

    def __post_init__(self) -> None:
        if self.interval <= 0:
            raise ValueError("interval must be positive")
        if self.failure_threshold < 1:
            raise ValueError("failure_threshold must be >= 1")
        if self.health_timeout <= 0:
            raise ValueError("health_timeout must be positive")
        if self.max_restarts < 0:
            raise ValueError("max_restarts must be >= 0")


@dataclass(slots=True)
class NodeStatus:
    node: DatabaseNode
    health: NodeHealth = NodeHealth.HEALTHY
    consecutive_failures: int = 0
    restarts: int = 0
    last_checked: float | None = field(default=None)


class ClusterController:
    """Monitors a cluster and drives failover and self-healing."""

    def __init__(self, nodes: Sequence[DatabaseNode], config: ControllerConfig | None = None) -> None:
        if not nodes:
            raise ValueError("at least one node is required")
        ids = [node.node_id for node in nodes]
        if len(set(ids)) != len(ids):
            raise ValueError(f"duplicate node ids: {ids}")
        primaries = [node for node in nodes if node.role is NodeRole.PRIMARY]
        if len(primaries) != 1:
            raise ValueError(f"expected exactly one primary, found {len(primaries)}")

        self.config = config or ControllerConfig()
        self._statuses: dict[str, NodeStatus] = {node.node_id: NodeStatus(node) for node in nodes}
        self.cycles = 0
        self.failovers = 0

    # ---- introspection ------------------------------------------------------
    @property
    def statuses(self) -> tuple[NodeStatus, ...]:
        return tuple(self._statuses.values())

    @property
    def primary(self) -> DatabaseNode | None:
        return next(
            (s.node for s in self._statuses.values() if s.node.role is NodeRole.PRIMARY), None
        )

    def snapshot(self) -> list[dict[str, object]]:
        return [
            {
                "node_id": s.node.node_id,
                "role": s.node.role.value,
                "health": s.health.value,
                "failures": s.consecutive_failures,
                "restarts": s.restarts,
            }
            for s in self._statuses.values()
        ]

    # ---- probing ------------------------------------------------------------
    async def _probe(self, status: NodeStatus) -> bool:
        node = status.node
        try:
            return await asyncio.wait_for(node.health_check(), timeout=self.config.health_timeout)
        except asyncio.TimeoutError:
            logger.warning("%s health check timed out after %.2fs", node.node_id, self.config.health_timeout)
        except Exception:  # noqa: BLE001 - a misbehaving node must not kill the control loop
            logger.exception("%s health check raised", node.node_id)
        return False

    def _record(self, status: NodeStatus, healthy: bool) -> None:
        status.last_checked = time.monotonic()
        if status.health is NodeHealth.QUARANTINED:
            return
        if healthy:
            if status.health is not NodeHealth.HEALTHY:
                logger.info("%s is healthy again", status.node.node_id)
            status.health = NodeHealth.HEALTHY
            status.consecutive_failures = 0
            return

        status.consecutive_failures += 1
        if status.consecutive_failures >= self.config.failure_threshold:
            if status.health is not NodeHealth.UNHEALTHY:
                logger.error(
                    "%s marked unhealthy after %d consecutive failures",
                    status.node.node_id, status.consecutive_failures,
                )
            status.health = NodeHealth.UNHEALTHY
        else:
            status.health = NodeHealth.SUSPECT

    # ---- remediation --------------------------------------------------------
    async def _failover(self, failed: NodeStatus) -> None:
        candidates = [
            s for s in self._statuses.values()
            if s.node.role is NodeRole.REPLICA and s.health is NodeHealth.HEALTHY
        ]
        if not candidates:
            logger.critical(
                "primary %s is down and no healthy replica is available; cluster is read-unavailable",
                failed.node.node_id,
            )
            return

        lags = await asyncio.gather(
            *(s.node.replication_lag() for s in candidates), return_exceptions=True
        )
        ranked = sorted(
            (
                (lag if isinstance(lag, float) else math.inf, s.node.node_id, s)
                for lag, s in zip(lags, candidates)
            ),
            key=lambda item: (item[0], item[1]),
        )

        try:
            await failed.node.demote()
        except Exception:  # noqa: BLE001 - fencing is best-effort on a dead node
            logger.exception("failed to fence old primary %s", failed.node.node_id)

        for lag, node_id, status in ranked:
            if math.isinf(lag):
                continue
            try:
                await status.node.promote()
            except Exception:  # noqa: BLE001
                logger.exception("promotion of %s failed; trying next candidate", node_id)
                continue
            self.failovers += 1
            logger.warning(
                "FAILOVER #%d: %s -> %s (replication lag %.3fs)",
                self.failovers, failed.node.node_id, node_id, lag,
            )
            return

        logger.critical("failover aborted: every candidate replica failed promotion")

    async def _heal(self, status: NodeStatus) -> None:
        node = status.node
        if status.restarts >= self.config.max_restarts:
            status.health = NodeHealth.QUARANTINED
            logger.error("%s quarantined after %d restarts", node.node_id, status.restarts)
            return

        status.health = NodeHealth.RECOVERING
        status.restarts += 1
        logger.info("restarting %s (attempt %d/%d)", node.node_id, status.restarts, self.config.max_restarts)
        try:
            await node.restart()
        except Exception:  # noqa: BLE001
            logger.exception("restart of %s failed", node.node_id)
            status.health = NodeHealth.UNHEALTHY
            return
        status.consecutive_failures = 0

    async def reconcile_once(self) -> None:
        """Run one probe -> failover -> heal cycle."""
        statuses = list(self._statuses.values())
        results = await asyncio.gather(*(self._probe(s) for s in statuses))
        for status, healthy in zip(statuses, results):
            self._record(status, healthy)

        primary = next((s for s in statuses if s.node.role is NodeRole.PRIMARY), None)
        if primary is not None and primary.health in {NodeHealth.UNHEALTHY, NodeHealth.QUARANTINED}:
            await self._failover(primary)

        for status in statuses:
            if status.health is NodeHealth.UNHEALTHY and status.node.role is NodeRole.REPLICA:
                await self._heal(status)

    async def run(self, stop_event: asyncio.Event | None = None, max_cycles: int | None = None) -> None:
        """Run the control loop until ``stop_event`` is set or ``max_cycles`` elapse."""
        if max_cycles is not None and max_cycles < 1:
            raise ValueError("max_cycles must be >= 1")
        stop = stop_event or asyncio.Event()
        logger.info(
            "controller started: %d nodes, interval=%.2fs, threshold=%d",
            len(self._statuses), self.config.interval, self.config.failure_threshold,
        )

        while not stop.is_set():
            started = time.monotonic()
            self.cycles += 1
            try:
                await self.reconcile_once()
            except Exception:  # noqa: BLE001 - keep the daemon alive
                logger.exception("reconcile cycle %d failed", self.cycles)
            logger.debug("cycle %d state: %s", self.cycles, self.snapshot())

            if max_cycles is not None and self.cycles >= max_cycles:
                break
            remaining = max(0.0, self.config.interval - (time.monotonic() - started))
            try:
                await asyncio.wait_for(stop.wait(), timeout=remaining)
            except asyncio.TimeoutError:
                continue

        logger.info("controller stopped after %d cycles, %d failovers", self.cycles, self.failovers)
