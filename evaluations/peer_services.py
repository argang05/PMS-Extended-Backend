from django.db import transaction
from django.utils import timezone
from django.contrib.auth import get_user_model
from .models import (
    EmployeeQuestionnaire,
    EmployeeQuestionnairePeerAssignment,
    EmployeeQuestionnaireStageSubmission
)

Employee = get_user_model()
MAX_PEERS = 3


class PeerServiceError(Exception):
    pass


def _positive_id(value, field="id"):
    if isinstance(value, bool):
        raise PeerServiceError(f"{field} must be a positive integer.")
    try:
        number = int(value)
    except (TypeError, ValueError, OverflowError):
        raise PeerServiceError(f"{field} must be a positive integer.")
    if str(value).strip() != str(number) or number <= 0:
        raise PeerServiceError(f"{field} must be a positive integer.")
    return number


def _lock_questionnaire(questionnaire_id):
    return EmployeeQuestionnaire.objects.select_related(
        "employee", "peer_reviewer", "review_cycle"
    ).select_for_update(of=("self",)).get(
        id=_positive_id(questionnaire_id, "questionnaire_id")
    )


def _assignment_ids(questionnaire):
    return list(
        EmployeeQuestionnairePeerAssignment.objects.filter(
            employee_questionnaire=questionnaire
        ).order_by("assigned_at", "id").values_list("peer_id", flat=True)
    )


def _effective_peer_ids(questionnaire):
    ids = _assignment_ids(questionnaire)
    legacy_id = questionnaire.peer_reviewer_id
    if legacy_id and legacy_id not in ids:
        ids.insert(0, legacy_id)
    return ids


def _ensure_legacy_assignment(questionnaire):
    legacy_id = questionnaire.peer_reviewer_id
    if not legacy_id:
        return

    if legacy_id == questionnaire.employee_id:
        raise PeerServiceError("Employees cannot review themselves.")

    if EmployeeQuestionnairePeerAssignment.objects.filter(
        employee_questionnaire=questionnaire,
        peer_id=legacy_id
    ).exists():
        return

    if len(_assignment_ids(questionnaire)) >= MAX_PEERS:
        raise PeerServiceError(
            "Legacy peer cannot be synchronized because three assignments already exist."
        )

    EmployeeQuestionnairePeerAssignment.objects.create(
        employee_questionnaire=questionnaire,
        peer_id=legacy_id
    )


def _validate_peer_ids(questionnaire, peer_ids):
    if not isinstance(peer_ids, list):
        raise PeerServiceError("peer_ids must be a list.")

    if not 1 <= len(peer_ids) <= MAX_PEERS:
        raise PeerServiceError("Select between one and three peer reviewers.")

    ids = [_positive_id(value, "peer_id") for value in peer_ids]

    if len(ids) != len(set(ids)):
        raise PeerServiceError("Duplicate peer reviewers are not allowed.")

    if questionnaire.employee_id in ids:
        raise PeerServiceError("Employees cannot review themselves.")

    valid_ids = set(
        Employee.objects.filter(
            id__in=ids,
            is_active=True
        ).values_list("id", flat=True)
    )

    invalid_ids = sorted(set(ids) - valid_ids)
    if invalid_ids:
        raise PeerServiceError(
            f"Invalid or inactive peer employee IDs: {invalid_ids}"
        )

    return ids


def _ensure_assignment_editable(questionnaire, desired_ids):
    current_ids = set(_effective_peer_ids(questionnaire))
    desired_ids = set(desired_ids)

    if questionnaire.status == "draft":
        raise PeerServiceError(
            "Peer reviewers can only be assigned after self-evaluation submission."
        )

    if questionnaire.status == "completed" and desired_ids != current_ids:
        raise PeerServiceError(
            "Peer assignments cannot be changed after questionnaire completion."
        )

    removed_ids = current_ids - desired_ids

    if removed_ids:
        existing_submissions = set(
            EmployeeQuestionnaireStageSubmission.objects.filter(
                employee_questionnaire=questionnaire,
                evaluator_type="peer",
                submitted_by_id__in=removed_ids
            ).values_list("submitted_by_id", flat=True)
        )

        if existing_submissions:
            raise PeerServiceError(
                f"Cannot remove peers with existing evaluation records: {sorted(existing_submissions)}"
            )


def _sync_legacy_peer(questionnaire, desired_ids):
    selected = desired_ids[0] if desired_ids else None

    if questionnaire.peer_reviewer_id == selected:
        return

    questionnaire.peer_reviewer_id = selected
    questionnaire.peer_requested_at = timezone.now() if selected else None
    questionnaire.save(update_fields=["peer_reviewer", "peer_requested_at"])


def _peer_details(questionnaire):
    assignments = list(
        EmployeeQuestionnairePeerAssignment.objects.filter(
            employee_questionnaire=questionnaire
        ).select_related("peer").order_by("assigned_at", "id")
    )

    assigned_ids = {assignment.peer_id for assignment in assignments}
    legacy_peer = questionnaire.peer_reviewer

    if legacy_peer and legacy_peer.id not in assigned_ids:
        assignments.insert(0, None)

    peer_ids = _effective_peer_ids(questionnaire)

    submissions = {
        submission.submitted_by_id: submission
        for submission in EmployeeQuestionnaireStageSubmission.objects.filter(
            employee_questionnaire=questionnaire,
            evaluator_type="peer",
            submitted_by_id__in=peer_ids
        )
    }

    results = []

    for assignment in assignments:
        peer = assignment.peer if assignment else legacy_peer
        submission = submissions.get(peer.id)

        if submission and submission.status == "submitted":
            status = "completed"
        elif submission:
            status = "draft"
        else:
            status = "pending"

        results.append({
            "assignment_id": assignment.id if assignment else None,
            "peer_id": peer.id,
            "employee_number": peer.employee_number,
            "full_name": peer.full_name,
            "department": peer.department,
            "status": status,
            "assigned_at": assignment.assigned_at if assignment else questionnaire.peer_requested_at
        })

    return results


def peer_progress(questionnaire):
    peers = _peer_details(questionnaire)
    assigned = len(peers)
    completed = sum(peer["status"] == "completed" for peer in peers)

    return {
        "assigned": assigned,
        "completed": completed,
        "pending": assigned - completed,
        "all_completed": assigned > 0 and completed == assigned,
        "peers": peers
    }


def _stage_completed(questionnaire, evaluator_type):
    return EmployeeQuestionnaireStageSubmission.objects.filter(
        employee_questionnaire=questionnaire,
        evaluator_type=evaluator_type,
        status="submitted"
    ).exists()


def review_stages_completed(questionnaire):
    if not _stage_completed(questionnaire, "self"):
        return False

    if not _stage_completed(questionnaire, "rm"):
        return False

    manager = questionnaire.employee.reporting_manager
    skip_exempt = str(getattr(manager, "band", "") or "").upper().startswith("SM")

    if not skip_exempt and not _stage_completed(questionnaire, "skip"):
        return False

    return True


def all_peers_completed(questionnaire):
    return peer_progress(questionnaire)["all_completed"]


def refresh_questionnaire_completion(questionnaire):
    if questionnaire.status == "draft":
        return False

    if not review_stages_completed(questionnaire):
        return False

    completed = all_peers_completed(questionnaire)
    new_status = "completed" if completed else "under_peer_review"

    if questionnaire.status != new_status:
        questionnaire.status = new_status
        questionnaire.save(update_fields=["status"])

    return completed


def _result(questionnaire, message):
    return {
        "message": message,
        "questionnaire_id": questionnaire.id,
        "questionnaire_status": questionnaire.status,
        "peer_review": peer_progress(questionnaire)
    }


@transaction.atomic
def assign_peers(questionnaire_id, peer_ids, employee):
    questionnaire = _lock_questionnaire(questionnaire_id)

    if questionnaire.employee_id != employee.id:
        raise PeerServiceError(
            "You cannot manage peer assignments for another employee."
        )

    desired_ids = _validate_peer_ids(questionnaire, peer_ids)
    _ensure_legacy_assignment(questionnaire)
    _ensure_assignment_editable(questionnaire, desired_ids)

    current_ids = set(_assignment_ids(questionnaire))
    desired_set = set(desired_ids)

    EmployeeQuestionnairePeerAssignment.objects.filter(
        employee_questionnaire=questionnaire,
        peer_id__in=current_ids - desired_set
    ).delete()

    for peer_id in desired_ids:
        if peer_id not in current_ids:
            EmployeeQuestionnairePeerAssignment.objects.create(
                employee_questionnaire=questionnaire,
                peer_id=peer_id
            )

    _sync_legacy_peer(questionnaire, desired_ids)
    refresh_questionnaire_completion(questionnaire)

    return _result(questionnaire, "Peer reviewers assigned successfully.")


@transaction.atomic
def add_peer(questionnaire_id, peer_id, employee):
    questionnaire = _lock_questionnaire(questionnaire_id)

    if questionnaire.employee_id != employee.id:
        raise PeerServiceError(
            "You cannot manage peer assignments for another employee."
        )

    peer_id = _positive_id(peer_id, "peer_id")
    current_ids = _effective_peer_ids(questionnaire)

    if peer_id in current_ids:
        raise PeerServiceError("This peer reviewer is already assigned.")

    if len(current_ids) >= MAX_PEERS:
        raise PeerServiceError("A maximum of three peer reviewers is allowed.")

    desired_ids = _validate_peer_ids(
        questionnaire, current_ids + [peer_id]
    )

    _ensure_legacy_assignment(questionnaire)
    _ensure_assignment_editable(questionnaire, desired_ids)

    EmployeeQuestionnairePeerAssignment.objects.create(
        employee_questionnaire=questionnaire,
        peer_id=peer_id
    )

    _sync_legacy_peer(questionnaire, desired_ids)
    refresh_questionnaire_completion(questionnaire)

    return _result(questionnaire, "Peer reviewer added successfully.")


@transaction.atomic
def remove_peer(questionnaire_id, peer_id, employee):
    questionnaire = _lock_questionnaire(questionnaire_id)

    if questionnaire.employee_id != employee.id:
        raise PeerServiceError(
            "You cannot manage peer assignments for another employee."
        )

    peer_id = _positive_id(peer_id, "peer_id")
    current_ids = _effective_peer_ids(questionnaire)

    if peer_id not in current_ids:
        raise PeerServiceError("Peer reviewer is not assigned.")

    desired_ids = [value for value in current_ids if value != peer_id]

    if not desired_ids:
        raise PeerServiceError(
            "At least one peer reviewer must remain assigned."
        )

    _ensure_legacy_assignment(questionnaire)
    _ensure_assignment_editable(questionnaire, desired_ids)

    EmployeeQuestionnairePeerAssignment.objects.filter(
        employee_questionnaire=questionnaire,
        peer_id=peer_id
    ).delete()

    _sync_legacy_peer(questionnaire, desired_ids)
    refresh_questionnaire_completion(questionnaire)

    return _result(questionnaire, "Peer reviewer removed successfully.")


def is_assigned_peer(questionnaire, employee):
    if not employee or not employee.is_authenticated:
        return False

    if questionnaire.employee_id == employee.id:
        return False

    return (
        EmployeeQuestionnairePeerAssignment.objects.filter(
            employee_questionnaire=questionnaire,
            peer=employee
        ).exists()
        or questionnaire.peer_reviewer_id == employee.id
    )


def peer_submission(questionnaire, employee, create=False):
    if not is_assigned_peer(questionnaire, employee):
        raise PeerServiceError(
            "You are not assigned to review this questionnaire."
        )

    if not create:
        return EmployeeQuestionnaireStageSubmission.objects.filter(
            employee_questionnaire=questionnaire,
            evaluator_type="peer",
            submitted_by=employee
        ).first()

    submission, _ = EmployeeQuestionnaireStageSubmission.objects.get_or_create(
        employee_questionnaire=questionnaire,
        evaluator_type="peer",
        submitted_by=employee,
        defaults={"status": "draft"}
    )

    return submission