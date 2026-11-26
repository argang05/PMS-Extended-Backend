from scoring.result_invalidation import (
    potential_dependency_state,
    empty_invalidation_result,
    invalidate_potential_dependencies as _invalidate_potential_dependencies
)

def invalidate_potential_dependencies(assessment, changed_by=None, preserve_score=False):
    return _invalidate_potential_dependencies(
        assessment, admin=changed_by, preserve_score=preserve_score
    )