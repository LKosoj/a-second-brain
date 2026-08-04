"""Control-plane contracts and routing helpers for d-brain."""

from d_brain.control_plane.registry import (
    CONTROL_PLANE_REGISTRY,
    get_question_workflow,
    iter_workflows,
    validate_control_plane_registry,
)
from d_brain.control_plane.router import (
    build_question_route_block,
    classify_question_route,
    iter_question_context_blocks,
    resolve_text_workflow,
)

__all__ = [
    "CONTROL_PLANE_REGISTRY",
    "build_question_route_block",
    "classify_question_route",
    "get_question_workflow",
    "iter_question_context_blocks",
    "iter_workflows",
    "resolve_text_workflow",
    "validate_control_plane_registry",
]
