"""Single source of truth for Studio preflight confirmations.

A Studio proposal records three user decisions before the input stage:
the interaction mode, the resource selection and the orchestration kind.
Historically each was stored under several keys (``*_preselected``,
``*_confirmed`` and ``resolved_clarifications``) and every caller combined
them slightly differently.  All reads and writes go through this module; the
legacy keys are still written so older browser tabs and stored sessions keep
working.
"""
from typing import Literal

PreflightStep = Literal['interaction_mode', 'resources', 'orchestration_kind', 'complete']
PREFLIGHT_FIELDS = ('interaction_mode', 'resource_selection', 'orchestration_kind')
INTERACTION_MODES = {'single_run', 'multi_turn'}
KINDS = {'crew', 'flow'}


def _resolved(proposal: dict | None) -> dict:
    return (proposal or {}).get('resolved_clarifications') or {}


def interaction_confirmed(proposal: dict | None) -> bool:
    proposal = proposal or {}
    return bool(proposal.get('interaction_mode_preselected') or _resolved(proposal).get('interaction_mode'))


def resources_confirmed(proposal: dict | None) -> bool:
    proposal = proposal or {}
    return bool(proposal.get('resource_selection_confirmed') or _resolved(proposal).get('resource_selection'))


def kind_confirmed(proposal: dict | None, request_kind: str | None = None) -> bool:
    """Whether the user chose Crew/Flow; ``request_kind`` is an explicit per-request choice."""
    proposal = proposal or {}
    return bool(
        request_kind in KINDS
        or proposal.get('kind_preselected') or proposal.get('kind_confirmed')
        or _resolved(proposal).get('orchestration_kind')
    )


def preflight_complete(proposal: dict | None) -> bool:
    return interaction_confirmed(proposal) and resources_confirmed(proposal) and kind_confirmed(proposal)


def next_preflight_step(proposal: dict | None, request_kind: str | None = None) -> PreflightStep:
    """The only step discovery may ask about next, in the fixed order."""
    if not interaction_confirmed(proposal):
        return 'interaction_mode'
    if not resources_confirmed(proposal):
        return 'resources'
    if not kind_confirmed(proposal, request_kind):
        return 'orchestration_kind'
    return 'complete'


def locked_interaction_mode(proposal: dict | None) -> str | None:
    """The confirmed interaction mode, or None when it is still open."""
    if not interaction_confirmed(proposal):
        return None
    proposal = proposal or {}
    mode = _resolved(proposal).get('interaction_mode')
    if mode not in INTERACTION_MODES:
        mode = proposal.get('interaction_mode')
    return mode if mode in INTERACTION_MODES else None


def mark_interaction(proposal: dict, mode: str | None = None) -> dict:
    if mode in INTERACTION_MODES:
        proposal['interaction_mode'] = mode
    proposal['interaction_mode_preselected'] = True
    return proposal


def mark_resources(proposal: dict, value: str = 'configured') -> dict:
    proposal['resource_selection_confirmed'] = True
    proposal.setdefault('resolved_clarifications', {})['resource_selection'] = value
    return proposal


def mark_kind(proposal: dict, kind: str | None = None) -> dict:
    if kind in KINDS:
        proposal['kind'] = kind
        proposal['recommended_kind'] = kind
    proposal['kind_preselected'] = True
    return proposal


def reset_preflight(proposal: dict, fields: list[str] | tuple[str, ...] | None = None) -> dict:
    """Reopen the named preflight decisions (all three when none are named)."""
    requested = [field for field in (fields or []) if field in PREFLIGHT_FIELDS]
    resets = set(requested or PREFLIGHT_FIELDS)
    resolved = dict(_resolved(proposal))
    if 'interaction_mode' in resets:
        resolved.pop('interaction_mode', None)
        proposal['interaction_mode_preselected'] = False
    if 'resource_selection' in resets:
        resolved.pop('resource_selection', None)
        proposal['resource_selection_confirmed'] = False
    if 'orchestration_kind' in resets:
        resolved.pop('orchestration_kind', None)
        proposal['kind_preselected'] = False
        proposal['kind_confirmed'] = False
    proposal['resolved_clarifications'] = resolved
    return proposal
