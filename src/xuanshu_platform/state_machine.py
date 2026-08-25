"""Shared execution state and transition rules for published applications.

The API, worker and Flow runtime all persist this shape inside ``Run`` and
conversation state.  Keeping the state machine independent from CrewAI makes
pause/resume deterministic and lets a restarted worker resume at a node
boundary instead of guessing from user-visible output.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


class RunStatus(StrEnum):
    COLLECTING = "collecting"
    READY = "ready"
    QUEUED = "queued"
    RUNNING = "running"
    WAITING_INPUT = "waiting_input"
    WAITING_APPROVAL = "waiting_approval"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class NodeStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    WAITING_INPUT = "waiting_input"
    WAITING_APPROVAL = "waiting_approval"
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"


class NodeCheckpoint(BaseModel):
    node_id: str
    status: NodeStatus = NodeStatus.PENDING
    attempt: int = 0
    input_hash: str = ""
    output: str = ""
    error: str = ""
    started_at: str | None = None
    completed_at: str | None = None
    version: int = 0

    def mark_running(self, input_hash: str) -> None:
        self.status = NodeStatus.RUNNING
        self.input_hash = input_hash
        self.attempt += 1
        self.version += 1
        self.started_at = datetime.now(UTC).isoformat()
        self.error = ""

    def mark_completed(self, output: str) -> None:
        self.status = NodeStatus.COMPLETED
        self.output = str(output or "")
        self.completed_at = datetime.now(UTC).isoformat()
        self.version += 1


class RuntimeCheckpoint(BaseModel):
    schema_version: int = 1
    status: RunStatus = RunStatus.READY
    current_node: str | None = None
    outputs: dict[str, str] = Field(default_factory=dict)
    nodes: dict[str, NodeCheckpoint] = Field(default_factory=dict)
    transition_version: int = 0
    waiting_input: dict[str, Any] | None = None
    waiting_approval: dict[str, Any] | None = None
    history_summary: str = ""
    history_tokens: int = 0

    @classmethod
    def from_resume(cls, value: dict[str, Any] | None) -> "RuntimeCheckpoint":
        raw = dict(value or {})
        checkpoint = raw.get("checkpoint")
        if isinstance(checkpoint, dict):
            try:
                parsed = cls.model_validate(checkpoint)
            except Exception:
                parsed = cls()
        else:
            parsed = cls()
        # Migrate the pre-checkpoint runtime shape without losing old runs.
        outputs = dict(parsed.outputs or raw.get("outputs") or {})
        parsed.outputs = outputs
        for node_id, output in outputs.items():
            node = parsed.nodes.setdefault(str(node_id), NodeCheckpoint(node_id=str(node_id)))
            if node.status not in {NodeStatus.COMPLETED, NodeStatus.SKIPPED}:
                node.status = NodeStatus.COMPLETED
                node.output = str(output or "")
        parsed.current_node = parsed.current_node or raw.get("pending_node")
        parsed.waiting_input = parsed.waiting_input or raw.get("waiting_input")
        parsed.history_summary = parsed.history_summary or str(raw.get("history_summary") or "")
        parsed.history_tokens = int(parsed.history_tokens or raw.get("history_tokens") or 0)
        # Older approval checkpoints marked an already-produced node as waiting.
        # Treat its persisted output as completion so resume cannot run it twice.
        if parsed.waiting_approval:
            approval_node_id = str(parsed.waiting_approval.get("node_id") or parsed.current_node or "")
            if approval_node_id:
                approval_node = parsed.node(approval_node_id)
                if approval_node.output or approval_node_id in parsed.outputs:
                    approval_node.status = NodeStatus.COMPLETED
        return parsed

    def input_hash(self, node_id: str, inputs: dict[str, Any], outputs: dict[str, Any]) -> str:
        payload = {"node": node_id, "inputs": inputs, "outputs": outputs}
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str).encode()
        return hashlib.sha256(encoded).hexdigest()

    def node(self, node_id: str) -> NodeCheckpoint:
        return self.nodes.setdefault(node_id, NodeCheckpoint(node_id=node_id))

    def completed(self, node_id: str) -> bool:
        return self.node(node_id).status in {NodeStatus.COMPLETED, NodeStatus.SKIPPED}

    def start_node(self, node_id: str, inputs: dict[str, Any], outputs: dict[str, Any]) -> NodeCheckpoint:
        self.current_node = node_id
        self.status = RunStatus.RUNNING
        self.transition_version += 1
        checkpoint = self.node(node_id)
        checkpoint.mark_running(self.input_hash(node_id, inputs, outputs))
        return checkpoint

    def complete_node(self, node_id: str, output: str) -> NodeCheckpoint:
        checkpoint = self.node(node_id)
        checkpoint.mark_completed(output)
        self.outputs[node_id] = str(output or "")
        self.current_node = None
        self.transition_version += 1
        return checkpoint

    def pause_for_input(self, node_id: str, request: dict[str, Any]) -> None:
        self.current_node = node_id
        self.status = RunStatus.WAITING_INPUT
        self.waiting_input = dict(request)
        self.node(node_id).status = NodeStatus.WAITING_INPUT
        self.transition_version += 1

    def pause_for_approval(self, node_id: str, request: dict[str, Any]) -> None:
        self.current_node = node_id
        self.status = RunStatus.WAITING_APPROVAL
        self.waiting_approval = dict(request)
        # Approval is requested after the node produced its candidate output.
        # Keep that node completed so resuming the run cannot execute it twice.
        node = self.node(node_id)
        if node.status != NodeStatus.COMPLETED:
            node.status = NodeStatus.WAITING_APPROVAL
        self.transition_version += 1

    def finish(self, output: str) -> None:
        self.status = RunStatus.COMPLETED
        self.current_node = None
        self.waiting_input = None
        self.waiting_approval = None
        self.transition_version += 1

    def fail(self, error: str) -> None:
        failed_node = self.current_node
        self.status = RunStatus.FAILED
        self.current_node = None
        self.transition_version += 1
        if failed_node:
            self.node(failed_node).status = NodeStatus.FAILED
            self.node(failed_node).error = str(error)

    def dump(self) -> dict[str, Any]:
        return self.model_dump(mode="json")
