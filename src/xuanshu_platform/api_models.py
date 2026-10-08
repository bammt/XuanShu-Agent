"""Request bodies accepted by the HTTP routes.
"""
from typing import Literal
from pydantic import (
    BaseModel,
    Field,
)


class WorkspaceIn(BaseModel): name: str


class UserIn(BaseModel): username: str; password: str = Field(min_length=8, max_length=128)


class PasswordResetIn(BaseModel): password: str = Field(min_length=8, max_length=128)


class ApiKeyIn(BaseModel): name: str


class InviteIn(BaseModel): username: str; can_edit: bool = False


class MemberPermissionIn(BaseModel): can_edit: bool


class ApprovalIn(BaseModel): outcome: str; feedback: str = ""


class DefaultModelIn(BaseModel):
    model_id: str
    model_type: str = 'chat'


class WorkflowRunIn(BaseModel):
    inputs: dict = {}
    attachments: dict[str, list[str]] = {}
    conversation_id: str = ''
    message: str = ''
    idempotency_key: str = ''
    # Builder preview runs the persisted draft. Normal runtime runs are
    # restricted to the explicit published snapshot.
    preview: bool = False


class ConversationCreateIn(BaseModel):
    preview: bool = False


class ExternalRunIn(BaseModel):
    inputs: dict = {}
    files: dict[str, list[str]] = {}
    conversation_id: str = ''
    user_id: str = ''
    new_conversation: bool = False
    message: str = ''
    idempotency_key: str = ''
    response_mode: Literal['blocking', 'streaming', 'async'] = 'blocking'
    wait_timeout_seconds: int = Field(default=120, ge=1, le=300)


class RunFeedbackIn(BaseModel): outcome: str; feedback: str = ''


class StudioSessionUpdate(BaseModel):
    proposal: dict | None = None
    kind: str | None = None
    title: str | None = None
    workflow: dict | None = None
    manual_changes: list[dict] = []


class StudioSessionCreate(BaseModel):
    kind: str = 'crew'
