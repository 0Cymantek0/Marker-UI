"""Real two-node PostgreSQL primary/physical-standby topology (PR83C2).

Provisions two real ``postgres:16-alpine`` containers — a primary and a
physical streaming standby built with ``pg_basebackup`` — on a dedicated
Docker bridge network, with the standby's data directory seeded through a
one-shot loader container into a named volume before the standby's first
start. The pair is fully introspectable: roles (``pg_is_in_recovery``),
replication state (``pg_stat_replication``), WAL receive/replay LSNs,
postmaster start times, and checkpoint timelines are all observed
directly — never inferred from container names.

The durability policy under test is executable, not prose: with
``synchronous=True`` the primary is configured with
``synchronous_standby_names = 'FIRST 1 (marker_standby)'`` and
``synchronous_commit = 'remote_apply'`` *after* the standby streams and
*before* any workload runs, and ``assert_policy_active`` refuses to
continue unless the live settings and ``sync_state`` prove it.

This module is test/control-plane glue only. It is not part of Marker
UI's supported production architecture; it exists so the failover drills
can perform a real ``pg_promote`` against real PostgreSQL.
"""

from __future__ import annotations

import asyncio
import socket
import subprocess
import time
from dataclasses import dataclass, field
from typing import Any

import pytest

#: Strict-mode gate for the failover suite: with the env set, a missing
#: Docker daemon or object store FAILS the tests instead of skipping, so
#: an invoked failover target can never pass silently.
STRICT_ENV = "MARKER_TEST_FAILOVER_STRICT"

DEFAULT_IMAGE = "postgres:16-alpine"
DEFAULT_PRIMARY_PORT = 55461
DEFAULT_STANDBY_PORT = 55462
SUPERUSER = "marker"
SUPERUSER_PASSWORD = "marker"
REPLICATION_USER = "fo_repl"
#: Throwaway local credentials for ephemeral test containers — not a
#: secret of any kind; the containers live only for the drill's lifetime.
REPLICATION_PASSWORD = "fo-repl-local"
STANDBY_APPLICATION_NAME = "marker_standby"

READY_TIMEOUT_SECONDS = 180.0
BASEBACKUP_TIMEOUT_SECONDS = 300.0

SYNCHRONOUS_STANDBY_NAMES = f"FIRST 1 ({STANDBY_APPLICATION_NAME})"
SYNCHRONOUS_COMMIT = "remote_apply"


def failover_strict_mode() -> bool:
    import os

    return os.getenv(STRICT_ENV, "").lower() in ("1", "true", "yes")


def _gate(message: str) -> None:
    """Skip (or fail in strict mode) with an actionable reason."""
    if failover_strict_mode():
        pytest.fail(f"strict mode refuses to skip: {message}")
    pytest.skip(message)


def require_failover_docker() -> None:
    """Docker daemon control is mandatory: the drill kills/promotes real
    PostgreSQL containers, and no mock or in-process substitute exists."""
    try:
        probe = subprocess.run(
            ["docker", "info"],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as error:
        _gate(
            "PR83C2 failover drills need a reachable Docker daemon to "
            f"provision the two-node PostgreSQL topology ({error})"
        )
        return
    if probe.returncode != 0:
        _gate(
            "PR83C2 failover drills need a reachable Docker daemon to "
            "provision the two-node PostgreSQL topology "
            f"(docker info exited {probe.returncode})"
        )


def _docker(args: list[str], *, check: bool = True, timeout: float | None = None):
    return subprocess.run(
        ["docker", *args],
        capture_output=True,
        text=True,
        check=check,
        timeout=timeout,
    )


def _docker_ok(args: list[str], *, timeout: float | None = None) -> bool:
    try:
        return _docker(args, check=False, timeout=timeout).returncode == 0
    except subprocess.TimeoutExpired:
        return False


def _assert_free_host_port(port: int, *, label: str) -> None:
    probe = socket.socket()
    try:
        probe.bind(("127.0.0.1", port))
    except OSError:
        raise SystemExit(
            f"host port {port} ({label}) is already occupied; the failover "
            "topology needs it free. Stop the occupying service or choose "
            "different ports."
        )
    finally:
        probe.close()


async def _connect(
    port: int,
    *,
    database: str = "postgres",
    user: str = SUPERUSER,
    password: str = SUPERUSER_PASSWORD,
    timeout: float = 10.0,
):
    import asyncpg

    return await asyncio.wait_for(
        asyncpg.connect(
            host="127.0.0.1",
            port=port,
            user=user,
            password=password,
            database=database,
        ),
        timeout=timeout,
    )


async def _fetchrow_sql(port: int, sql: str) -> dict[str, Any] | None:
    conn = await _connect(port)
    try:
        row = await conn.fetchrow(sql)
        return dict(row) if row is not None else None
    finally:
        await conn.close()


def _lsn_to_int(lsn: str | None) -> int | None:
    if not lsn:
        return None
    high, _, low = lsn.partition("/")
    return (int(high, 16) << 32) | int(low, 16)


@dataclass
class FailoverCluster:
    """One primary/standby PostgreSQL pair with real promotion control."""

    primary_port: int = DEFAULT_PRIMARY_PORT
    standby_port: int = DEFAULT_STANDBY_PORT
    image: str = DEFAULT_IMAGE
    #: Name tag separating concurrent/fresh clusters ("" for the default).
    tag: str = ""
    #: Configure synchronous replication (the durable profile). When
    #: False the pair stays asynchronous — the declared-lossy lane.
    synchronous: bool = True
    keep: bool = False

    facts: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        suffix = f"-{self.tag}" if self.tag else ""
        self.primary_container = f"marker-fo{suffix}-primary"
        self.standby_container = f"marker-fo{suffix}-standby"
        self.network_name = f"marker-fo{suffix}-net"
        self.standby_volume = f"marker-fo{suffix}-standby-data"

    # -- URLs ---------------------------------------------------------------

    @property
    def primary_admin_url(self) -> str:
        return (
            f"postgresql+asyncpg://{SUPERUSER}:{SUPERUSER_PASSWORD}"
            f"@127.0.0.1:{self.primary_port}/postgres"
        )

    @property
    def standby_admin_url(self) -> str:
        return (
            f"postgresql+asyncpg://{SUPERUSER}:{SUPERUSER_PASSWORD}"
            f"@127.0.0.1:{self.standby_port}/postgres"
        )

    def url_for(self, node: str, database: str) -> str:
        port = self.primary_port if node == "primary" else self.standby_port
        return (
            f"postgresql+asyncpg://{SUPERUSER}:{SUPERUSER_PASSWORD}"
            f"@127.0.0.1:{port}/{database}"
        )

    # -- Provisioning ---------------------------------------------------------

    def _cleanup_stale(self) -> None:
        for name in (self.primary_container, self.standby_container):
            _docker(["rm", "-f", name], check=False, timeout=60)
        _docker(["network", "rm", self.network_name], check=False)
        _docker(["volume", "rm", "-f", self.standby_volume], check=False)

    async def provision(self) -> None:
        require_failover_docker()
        self._cleanup_stale()
        _assert_free_host_port(self.primary_port, label="primary")
        _assert_free_host_port(self.standby_port, label="standby")

        _docker(["network", "create", self.network_name])
        try:
            _docker(
                [
                    "run",
                    "-d",
                    "--name",
                    self.primary_container,
                    "--network",
                    self.network_name,
                    "-p",
                    f"127.0.0.1:{self.primary_port}:5432",
                    "-e",
                    f"POSTGRES_USER={SUPERUSER}",
                    "-e",
                    f"POSTGRES_PASSWORD={SUPERUSER_PASSWORD}",
                    "-e",
                    "POSTGRES_DB=postgres",
                    self.image,
                ]
            )
            await self._wait_node_ready(self.primary_port)

            # Replication role + matching pg_hba entry, then reload.
            conn = await _connect(self.primary_port)
            try:
                await conn.execute(
                    f"CREATE ROLE {REPLICATION_USER} WITH REPLICATION LOGIN "
                    f"PASSWORD '{REPLICATION_PASSWORD}'"
                )
            finally:
                await conn.close()
            _docker(
                [
                    "exec",
                    self.primary_container,
                    "sh",
                    "-c",
                    "printf '%s\\n' "
                    f"'host replication {REPLICATION_USER} all scram-sha-256' "
                    ">> /var/lib/postgresql/data/pg_hba.conf",
                ]
            )
            conn = await _connect(self.primary_port)
            try:
                await conn.execute("SELECT pg_reload_conf()")
            finally:
                await conn.close()

            # Seed the standby data volume with a physical base backup via
            # a one-shot loader container on the shared network. -R writes
            # standby.signal + primary_conninfo carrying the application
            # name the synchronous policy matches on.
            _docker(["volume", "create", self.standby_volume])
            loader_script = (
                "chown postgres:postgres /data && "
                "su postgres -c 'pg_basebackup "
                '-d "host=' + self.primary_container
                + " port=5432 user=" + REPLICATION_USER
                + " password=" + REPLICATION_PASSWORD
                + " application_name=" + STANDBY_APPLICATION_NAME
                + '" -D /data -Fp -Xs -R\''
            )
            _docker(
                [
                    "run",
                    "--rm",
                    "--network",
                    self.network_name,
                    "-v",
                    f"{self.standby_volume}:/data",
                    "-e",
                    f"PGPASSWORD={REPLICATION_PASSWORD}",
                    self.image,
                    "sh",
                    "-c",
                    loader_script,
                ],
                timeout=BASEBACKUP_TIMEOUT_SECONDS,
            )

            _docker(
                [
                    "run",
                    "-d",
                    "--name",
                    self.standby_container,
                    "--network",
                    self.network_name,
                    "-p",
                    f"127.0.0.1:{self.standby_port}:5432",
                    "-v",
                    f"{self.standby_volume}:/var/lib/postgresql/data",
                    "-e",
                    f"POSTGRES_USER={SUPERUSER}",
                    "-e",
                    f"POSTGRES_PASSWORD={SUPERUSER_PASSWORD}",
                    "-e",
                    "POSTGRES_DB=postgres",
                    self.image,
                ]
            )
            await self._wait_node_ready(self.standby_port)
            await self._wait_streaming()
            if self.synchronous:
                await self.enable_synchronous()
        except BaseException:
            # A half-built topology must never linger holding the ports.
            self.teardown()
            raise

        primary = await self.node_facts("primary")
        standby = await self.node_facts("standby")
        if primary["in_recovery"] is not False or standby["in_recovery"] is not True:
            raise SystemExit(
                f"role sanity failed: primary in_recovery={primary['in_recovery']} "
                f"standby in_recovery={standby['in_recovery']}"
            )

    async def _wait_node_ready(self, port: int) -> None:
        deadline = time.monotonic() + READY_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            try:
                conn = await _connect(port, timeout=3.0)
            except Exception:
                await asyncio.sleep(1.0)
                continue
            await conn.close()
            return
        raise SystemExit(
            f"PostgreSQL node on 127.0.0.1:{port} did not become ready within "
            f"{READY_TIMEOUT_SECONDS:.0f}s"
        )

    async def _wait_streaming(self) -> None:
        deadline = time.monotonic() + READY_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            row = await _fetchrow_sql(
                self.primary_port,
                "SELECT state FROM pg_stat_replication "
                f"WHERE application_name = '{STANDBY_APPLICATION_NAME}'",
            )
            if row is not None and row["state"] == "streaming":
                return
            await asyncio.sleep(1.0)
        raise SystemExit(
            f"standby never appeared as streaming in pg_stat_replication within "
            f"{READY_TIMEOUT_SECONDS:.0f}s"
        )

    async def enable_synchronous(self) -> None:
        """Apply the durable profile AFTER streaming exists, so no
        workload can be acknowledged before a sync standby is attached."""
        conn = await _connect(self.primary_port)
        try:
            await conn.execute(
                "ALTER SYSTEM SET synchronous_standby_names = "
                f"'{SYNCHRONOUS_STANDBY_NAMES}'"
            )
            await conn.execute(
                f"ALTER SYSTEM SET synchronous_commit = '{SYNCHRONOUS_COMMIT}'"
            )
            await conn.execute("SELECT pg_reload_conf()")
        finally:
            await conn.close()
        await self.wait_synchronous()

    async def wait_synchronous(self, *, timeout: float = READY_TIMEOUT_SECONDS) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            row = await self.replication_facts()
            if row is not None and row.get("sync_state") == "sync":
                return
            await asyncio.sleep(0.5)
        raise SystemExit(
            "standby never reached sync_state='sync'; refusing to run a "
            "durability drill without the declared synchronous policy"
        )

    # -- Introspection ---------------------------------------------------------

    async def node_facts(self, node: str) -> dict[str, Any]:
        port = self.primary_port if node == "primary" else self.standby_port
        lsn_expr = (
            "pg_current_wal_lsn()" if node == "primary" else "pg_last_wal_receive_lsn()"
        )
        return {
            "node": node,
            "container": (
                self.primary_container if node == "primary" else self.standby_container
            ),
            "port": port,
            **(
                await _fetchrow_sql(
                    port,
                    "SELECT version() AS banner, "
                    "pg_is_in_recovery() AS in_recovery, "
                    f"{lsn_expr} AS wal_lsn, "
                    "pg_postmaster_start_time() AS postmaster_start_time, "
                    "timeline_id FROM pg_control_checkpoint()",
                )
                or {}
            ),
        }

    async def replication_facts(self) -> dict[str, Any] | None:
        row = await _fetchrow_sql(
            self.primary_port,
            "SELECT application_name, state, sync_state, "
            "sent_lsn::text AS sent_lsn, write_lsn::text AS write_lsn, "
            "flush_lsn::text AS flush_lsn, replay_lsn::text AS replay_lsn "
            f"FROM pg_stat_replication WHERE application_name = "
            f"'{STANDBY_APPLICATION_NAME}'",
        )
        return row

    async def effective_policy(self) -> dict[str, str]:
        row = await _fetchrow_sql(
            self.primary_port,
            "SELECT current_setting('synchronous_commit') AS synchronous_commit, "
            "current_setting('synchronous_standby_names') "
            "AS synchronous_standby_names",
        )
        return dict(row or {})

    async def assert_policy_active(self) -> None:
        """Executable durability declaration: refuse to continue unless
        the live cluster proves the declared synchronous profile."""
        policy = await self.effective_policy()
        assert policy.get("synchronous_commit") == SYNCHRONOUS_COMMIT, policy
        assert policy.get("synchronous_standby_names") == SYNCHRONOUS_STANDBY_NAMES, (
            policy
        )
        replication = await self.replication_facts()
        assert replication is not None, "no replication row while policy active"
        assert replication["state"] == "streaming", replication
        assert replication["sync_state"] == "sync", replication

    async def head_lsn(self, node: str) -> str | None:
        port = self.primary_port if node == "primary" else self.standby_port
        row = await _fetchrow_sql(
            port,
            "SELECT pg_current_wal_lsn()::text AS lsn" if node == "primary"
            else "SELECT pg_last_wal_replay_lsn()::text AS lsn",
        )
        return (row or {}).get("lsn")

    async def wait_standby_replayed(
        self, primary_lsn: str, *, timeout: float = 60.0
    ) -> int:
        """Block until the standby replayed past ``primary_lsn``.

        This is the pre-fault durability condition: the caller records the
        primary LSN *after* an acknowledgement, then proves the standby
        possessed that truth BEFORE the fault — never after it.
        """
        target = _lsn_to_int(primary_lsn)
        assert target is not None, primary_lsn
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            row = await _fetchrow_sql(
                self.standby_port,
                "SELECT pg_last_wal_replay_lsn()::text AS lsn",
            )
            replayed = _lsn_to_int((row or {}).get("lsn"))
            if replayed is not None and replayed >= target:
                return replayed
            await asyncio.sleep(0.25)
        raise SystemExit(
            f"standby did not replay past {primary_lsn} within {timeout:.0f}s "
            "before the fault; the durability condition was never established"
        )

    # -- Faults and promotion ---------------------------------------------------

    async def kill_primary(self) -> float:
        """Hard-kill the primary container (SIGKILL, no graceful
        checkpoint, no role handoff) and prove it refuses connections."""
        epoch = time.time()
        _docker(["kill", self.primary_container], timeout=60)
        self.facts["kill_epoch"] = epoch
        await asyncio.sleep(0.5)
        refused = False
        try:
            conn = await _connect(self.primary_port, timeout=3.0)
            await conn.close()
        except Exception:
            refused = True
        assert refused, "old primary still accepts connections after the kill"
        return epoch

    async def promote(self) -> dict[str, Any]:
        """Promote the standby via ``pg_promote(wait=true)`` and prove the
        role change: recovery exits, the server accepts writes, the
        checkpoint timeline advances, and the postmaster process is the
        SAME one (promotion, not a restart)."""
        before = await self.node_facts("standby")
        assert before["in_recovery"] is True, before

        started = time.time()
        conn = await _connect(self.standby_port)
        try:
            promoted = await conn.fetchval("SELECT pg_promote(true, 60)")
            assert promoted is True
        finally:
            await conn.close()
        completed = time.time()

        # Materialize the new timeline in the control file, then observe.
        conn = await _connect(self.standby_port)
        try:
            await conn.execute("CHECKPOINT")
        finally:
            await conn.close()
        after = await self.node_facts("standby")

        assert after["in_recovery"] is False, after
        assert after["postmaster_start_time"] == before["postmaster_start_time"], (
            "postmaster restarted: this was a container restart, not a promotion"
        )
        assert int(after["timeline_id"]) == int(before["timeline_id"]) + 1, (
            f"timeline {before['timeline_id']} -> {after['timeline_id']} "
            "did not advance by one promotion"
        )

        # Writable proof on the promoted authority itself.
        conn = await _connect(self.standby_port)
        try:
            await conn.execute(
                "CREATE TABLE IF NOT EXISTS marker_promotion_probe(id int primary key)"
            )
            await conn.execute("INSERT INTO marker_promotion_probe VALUES (1)")
            count = await conn.fetchval("SELECT count(*) FROM marker_promotion_probe")
            await conn.execute("DROP TABLE marker_promotion_probe")
            assert count == 1
        finally:
            await conn.close()

        facts = {
            "promote_started_epoch": started,
            "promote_completed_epoch": completed,
            "promote_wall_seconds": completed - started,
            "timeline_before": int(before["timeline_id"]),
            "timeline_after": int(after["timeline_id"]),
            "postmaster_start_time": str(after["postmaster_start_time"]),
            "promoted_banner": after["banner"],
            "promoted_writable": True,
            "promotion_not_restart": True,
        }
        self.facts.update(facts)
        return facts

    async def restart_old_primary(self) -> dict[str, Any]:
        """Restart the killed old primary WITHOUT rejoin: it comes back as
        an independent writable server on the old timeline — the split-
        authority hazard. The drills prove Marker UI's truth boundary
        detects its staleness and never consults it."""
        _docker(["start", self.primary_container], timeout=120)
        await self._wait_node_ready(self.primary_port)
        return await self.node_facts("primary")

    async def stop_old_primary(self) -> None:
        _docker(["kill", self.primary_container], timeout=60)

    async def stop_standby(self) -> float:
        epoch = time.time()
        _docker(["stop", self.standby_container], timeout=120)
        return epoch

    async def start_standby(self, *, expect_streaming: bool = True) -> float:
        epoch = time.time()
        _docker(["start", self.standby_container], timeout=120)
        await self._wait_node_ready(self.standby_port)
        if expect_streaming:
            await self._wait_streaming()
            if self.synchronous:
                await self.wait_synchronous()
        return epoch

    def teardown(self) -> None:
        if self.keep:
            return
        for name in (self.primary_container, self.standby_container):
            _docker(["rm", "-f", name], check=False, timeout=90)
        _docker(["network", "rm", self.network_name], check=False)
        _docker(["volume", "rm", "-f", self.standby_volume], check=False)
