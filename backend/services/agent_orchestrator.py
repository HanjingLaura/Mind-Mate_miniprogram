"""Mind-Mate Agent 闭环的确定性编排与审计记录。"""

from __future__ import annotations

import json
import secrets
from datetime import datetime, timedelta

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from config import settings
from models import AgentRun


STAGE_ORDER = ("understand", "plan", "act", "verify", "reflect")


def _stage_history(run: AgentRun) -> list[str]:
    try:
        snapshot = json.loads(run.input_snapshot or "{}")
    except (TypeError, ValueError):
        snapshot = {}
    history = snapshot.get("stage_history") or [run.stage]
    if history[-1] != run.stage:
        history.append(run.stage)
    return history


def advance_agent_stage(db: Session, run: AgentRun, stage: str) -> AgentRun:
    """Persist the next auditable stage before the next side effect."""
    _validate_next_stage(run.stage, stage)
    try:
        snapshot = json.loads(run.input_snapshot or "{}")
    except (TypeError, ValueError):
        snapshot = {}
    history = snapshot.setdefault("stage_history", [run.stage])
    if not history or history[-1] != stage:
        history.append(stage)
    _fenced_update(db, run, {
        "stage": stage,
        "input_snapshot": json.dumps(snapshot, ensure_ascii=False),
    })
    return run


def verify_agent_run(db: Session, run: AgentRun, checks: dict[str, bool]) -> dict:
    """Verify flushed database facts inside the final atomic transaction."""
    _validate_next_stage(run.stage, "verify")
    snapshot = json.loads(run.input_snapshot or "{}")
    history = snapshot.setdefault("stage_history", [run.stage])
    if not history or history[-1] != "verify":
        history.append("verify")
    verification = {"checks": checks, "passed": bool(checks) and all(checks.values())}
    if not verification["passed"]:
        raise RuntimeError("Agent result verification failed")
    _fenced_update(db, run, {
        "stage": "verify",
        "input_snapshot": json.dumps(snapshot, ensure_ascii=False),
        "output_snapshot": json.dumps({"verification": verification}, ensure_ascii=False),
    })
    return verification


def _assert_run_lease(db: Session, run: AgentRun) -> None:
    """Prevent a stale worker from finalizing a reclaimed run."""
    current = db.query(AgentRun).filter(
        AgentRun.id == run.id,
        AgentRun.status == "running",
        AgentRun.lease_token == run.lease_token,
    ).first()
    if current is None:
        raise RuntimeError("Agent run lease is no longer owned")


def _fenced_update(db: Session, run: AgentRun, values: dict) -> None:
    """Update a run only while this worker still owns its fencing token."""
    updated = db.query(AgentRun).filter(
        AgentRun.id == run.id,
        AgentRun.status == "running",
        AgentRun.lease_token == run.lease_token,
    ).update(values, synchronize_session=False)
    if updated != 1:
        db.rollback()
        raise RuntimeError("Agent run lease is no longer owned")
    for key, value in values.items():
        setattr(run, key, value)


def _validate_next_stage(current: str, target: str) -> None:
    if target not in STAGE_ORDER or current not in STAGE_ORDER:
        raise ValueError(f"Unknown AgentRun stage: {current} -> {target}")
    if STAGE_ORDER.index(target) not in {STAGE_ORDER.index(current), STAGE_ORDER.index(current) + 1}:
        raise ValueError(f"Invalid AgentRun stage transition: {current} -> {target}")


def recover_stale_agent_runs(db: Session) -> int:
    """Close abandoned audit records without pretending their work succeeded."""
    now = datetime.utcnow()
    cutoff = now - timedelta(seconds=settings.AGENT_RUN_LEASE_SECONDS)
    runs = db.query(AgentRun).filter(
        AgentRun.status == "running", AgentRun.started_at < cutoff,
    ).all()
    recovered = 0
    for run in runs:
        updated = db.query(AgentRun).filter(
            AgentRun.id == run.id,
            AgentRun.status == "running",
            AgentRun.started_at == run.started_at,
            AgentRun.lease_token == run.lease_token,
        ).update({
            "status": "interrupted",
            "error_message": "Execution lease expired; result requires reconciliation.",
            "finished_at": now,
        }, synchronize_session=False)
        recovered += updated
    if recovered:
        db.commit()
    return recovered


def begin_agent_run(
    db: Session,
    *,
    user_id: int,
    run_key: str,
    agent_name: str,
    stage: str,
    trigger: str,
    input_snapshot: dict,
) -> tuple[AgentRun, bool]:
    """Claim a run key; callers skip work when a completed run already exists."""
    existing = db.query(AgentRun).filter(AgentRun.user_id == user_id, AgentRun.run_key == run_key).first()
    if existing and existing.status == "completed":
        return existing, False
    if existing and existing.status == "running":
        started_at = existing.started_at or datetime.utcnow()
        if datetime.utcnow() - started_at < timedelta(seconds=settings.AGENT_RUN_LEASE_SECONDS):
            return existing, False
    if existing:
        old_status = existing.status
        old_started_at = existing.started_at
        old_lease_token = existing.lease_token
        now = datetime.utcnow()
        updated = db.query(AgentRun).filter(
            AgentRun.id == existing.id,
            AgentRun.status == old_status,
            AgentRun.started_at == old_started_at,
            AgentRun.lease_token == old_lease_token,
        ).update({
            "status": "running",
            "error_message": "",
            "started_at": now,
            "lease_token": secrets.token_urlsafe(24),
            "finished_at": None,
            "input_snapshot": json.dumps(input_snapshot, ensure_ascii=False),
        }, synchronize_session=False)
        if updated == 1:
            db.commit()
            db.refresh(existing)
            return existing, True
        db.rollback()
        current = db.query(AgentRun).filter(AgentRun.id == existing.id).first()
        if current:
            return current, False

    run = AgentRun(
        user_id=user_id,
        run_key=run_key,
        agent_name=agent_name,
        stage=stage,
        trigger=trigger,
        status="running",
        input_snapshot=json.dumps(input_snapshot, ensure_ascii=False),
        lease_token=secrets.token_urlsafe(24),
    )
    db.add(run)
    try:
        db.commit()
        db.refresh(run)
    except IntegrityError:
        db.rollback()
        existing = db.query(AgentRun).filter(AgentRun.user_id == user_id, AgentRun.run_key == run_key).first()
        if existing:
            return existing, False
        raise
    return run, True


def finish_agent_run(
    db: Session,
    run: AgentRun,
    *,
    status: str,
    output_snapshot: dict | None = None,
    provider: str = "",
    error_message: str = "",
    stage: str | None = None,
    commit: bool = True,
) -> AgentRun:
    _assert_run_lease(db, run)
    if stage:
        _validate_next_stage(run.stage, stage)
        snapshot = json.loads(run.input_snapshot or "{}")
        history = snapshot.setdefault("stage_history", [run.stage])
        if not history or history[-1] != stage:
            history.append(stage)
        run.input_snapshot = json.dumps(snapshot, ensure_ascii=False)
        run.stage = stage
    output = dict(output_snapshot or {})
    output.setdefault("stage_history", _stage_history(run))
    values = {
        "stage": run.stage,
        "input_snapshot": run.input_snapshot,
        "status": status,
        "provider": provider,
        "output_snapshot": json.dumps(output, ensure_ascii=False),
        "error_message": error_message[:2000],
        "finished_at": datetime.utcnow(),
    }
    if commit:
        updated = db.query(AgentRun).filter(
            AgentRun.id == run.id,
            AgentRun.status == "running",
            AgentRun.lease_token == run.lease_token,
        ).update(values, synchronize_session=False)
        if updated != 1:
            db.rollback()
            raise RuntimeError("Agent run lease is no longer owned")
        db.commit()
        db.refresh(run)
    else:
        run.status = status
        run.provider = provider
        run.output_snapshot = values["output_snapshot"]
        run.error_message = values["error_message"]
        run.finished_at = values["finished_at"]
        db.flush()
    return run
