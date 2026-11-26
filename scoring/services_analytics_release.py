from django.db import transaction
from django.utils import timezone
from evaluations.models import EmployeeQuestionnaireStageSubmission, EmployeeQuestionnairePeerAssignment
from .models import (
    EmployeeAnalyticsRelease, EmployeePerformanceScore,
    EmployeePastPerformanceIndex, EmployeePotentialScore,
    EmployeeNineBoxPlacement
)
from .scoring_locks import lock_questionnaire
from .score_readiness import get_scoring_readiness

def get_or_create_analytics_release(questionnaire):
    release, _ = EmployeeAnalyticsRelease.objects.get_or_create(employee_questionnaire_id=questionnaire.pk)
    return release

def _skip_required(questionnaire):
    manager = questionnaire.employee.reporting_manager
    return not str(getattr(manager, "band", "") or "").upper().startswith("SM")

def _peer_status(questionnaire):
    peer_ids = set(EmployeeQuestionnairePeerAssignment.objects.filter(employee_questionnaire_id=questionnaire.pk).values_list("peer_id", flat=True))
    if not peer_ids and questionnaire.peer_reviewer_id:
        peer_ids.add(questionnaire.peer_reviewer_id)
    submitted = set(EmployeeQuestionnaireStageSubmission.objects.filter(
        employee_questionnaire_id=questionnaire.pk, evaluator_type="peer",
        status="submitted", submitted_by_id__in=peer_ids
    ).values_list("submitted_by_id", flat=True))
    return {
        "assigned_count": len(peer_ids),
        "submitted_count": len(submitted),
        "is_complete": bool(peer_ids) and submitted == peer_ids
    }

def _submission_status(questionnaire):
    types = set(EmployeeQuestionnaireStageSubmission.objects.filter(employee_questionnaire_id=questionnaire.pk, status="submitted").values_list("evaluator_type", flat=True))
    return {evaluator: evaluator in types for evaluator in ("self", "rm", "skip", "peer")}

def _unique_requirements(requirements):
    seen, unique = set(), []
    for requirement in requirements:
        key = str(requirement)
        if key not in seen:
            seen.add(key)
            unique.append(requirement)
    return unique

def get_analytics_readiness(questionnaire, scoring=None):
    questionnaire = type(questionnaire).objects.select_related("employee", "employee__reporting_manager", "review_cycle", "peer_reviewer").get(pk=questionnaire.pk)
    stages = _submission_status(questionnaire)
    peers = _peer_status(questionnaire)
    skip_required = _skip_required(questionnaire)
    if scoring is None:
        scoring = get_scoring_readiness(questionnaire)
    missing = []
    if not stages["self"]:
        missing.append("Self evaluation has not been submitted.")
    if not stages["rm"]:
        missing.append("Reporting manager evaluation has not been submitted.")
    if skip_required and not stages["skip"]:
        missing.append("Skip-level evaluation has not been submitted.")
    if not peers["is_complete"]:
        missing.append(f"Peer evaluations incomplete: {peers['submitted_count']}/{peers['assigned_count']} submitted.")
    core_states = [scoring[key] for key in ("performance", "potential", "ppi", "ninebox")]
    for state in core_states:
        missing.extend(state["missing_requirements"])
    missing = _unique_requirements(missing)
    is_ready = not missing and all(state["is_current"] for state in core_states)
    release = EmployeeAnalyticsRelease.objects.filter(employee_questionnaire_id=questionnaire.pk).first()
    is_released = bool(release and release.is_released_to_employee)
    return {
        "is_ready": is_ready,
        "can_release": is_ready,
        "can_unrelease": is_released,
        "is_released_to_employee": is_released,
        "release_is_current": is_released and is_ready,
        "missing_requirements": missing,
        "has_processed_score": scoring["performance"]["exists"],
        "has_ppi": scoring["ppi"]["exists"],
        "has_potential": scoring["potential"]["exists"],
        "has_nine_box": scoring["ninebox"]["exists"],
        "has_appraisal": scoring["appraisal"]["exists"],
        "performance_is_current": scoring["performance"]["is_current"],
        "ppi_is_current": scoring["ppi"]["is_current"],
        "potential_is_current": scoring["potential"]["is_current"],
        "nine_box_is_current": scoring["ninebox"]["is_current"],
        "appraisal_is_current": scoring["appraisal"]["is_current"],
        "has_self_feedback": stages["self"],
        "has_rm_feedback": stages["rm"],
        "has_skip_feedback": stages["skip"],
        "has_peer_feedback": peers["is_complete"],
        "peer_assigned_count": peers["assigned_count"],
        "peer_submitted_count": peers["submitted_count"],
        "skip_required": skip_required
    }

@transaction.atomic
def release_analytics_to_employee(questionnaire, hr_user):
    questionnaire = lock_questionnaire(questionnaire.pk)
    readiness = get_analytics_readiness(questionnaire)
    if not readiness["is_ready"]:
        return {
            "success": False, "changed": False,
            "message": "Analytics data is not ready for release.",
            "missing_requirements": readiness["missing_requirements"]
        }
    release = get_or_create_analytics_release(questionnaire)
    if release.is_released_to_employee:
        return {
            "success": True, "changed": False,
            "message": "Analytics data is already released.",
            "missing_requirements": []
        }
    release.is_released_to_employee = True
    release.released_at = timezone.now()
    release.released_by = hr_user
    release.save(update_fields=["is_released_to_employee", "released_at", "released_by", "updated_at"])
    return {
        "success": True, "changed": True,
        "message": "Analytics data released successfully.",
        "missing_requirements": []
    }

@transaction.atomic
def unrelease_analytics_to_employee(questionnaire, hr_user):
    questionnaire = lock_questionnaire(questionnaire.pk)
    release = EmployeeAnalyticsRelease.objects.select_for_update().filter(employee_questionnaire_id=questionnaire.pk).first()
    if release is None or not release.is_released_to_employee:
        return {
            "success": True, "changed": False,
            "message": "Analytics data is already unreleased."
        }
    release.is_released_to_employee = False
    release.unreleased_at = timezone.now()
    release.unreleased_by = hr_user
    release.save(update_fields=["is_released_to_employee", "unreleased_at", "unreleased_by", "updated_at"])
    return {
        "success": True, "changed": True,
        "message": "Analytics data unreleased successfully."
    }

def _evaluator_name(submission, questionnaire):
    if submission.evaluator_type == "self":
        return questionnaire.employee.full_name
    if submission.submitted_by:
        return getattr(submission.submitted_by, "full_name", None) or str(submission.submitted_by)
    return {
        "rm": "Reporting Manager",
        "skip": "Skip-Level Manager",
        "peer": "Peer Reviewer"
    }.get(submission.evaluator_type, submission.evaluator_type)

def build_employee_analytics_report(questionnaire):
    release = EmployeeAnalyticsRelease.objects.filter(employee_questionnaire_id=questionnaire.pk).first()
    if release is None:
        release = EmployeeAnalyticsRelease(employee_questionnaire_id=questionnaire.pk)
    score = EmployeePerformanceScore.objects.filter(employee_questionnaire_id=questionnaire.pk).first()
    ppi = EmployeePastPerformanceIndex.objects.filter(employee_id=questionnaire.employee_id, review_cycle_id=questionnaire.review_cycle_id).first()
    potential = EmployeePotentialScore.objects.filter(employee_questionnaire_id=questionnaire.pk).first()
    nine_box = EmployeeNineBoxPlacement.objects.filter(employee_questionnaire_id=questionnaire.pk).first()
    submissions = EmployeeQuestionnaireStageSubmission.objects.filter(employee_questionnaire_id=questionnaire.pk, status="submitted").select_related("submitted_by").order_by("submitted_at", "id")
    visibility = {
        "self": (release.show_self_feedback, release.show_self_scope_of_improvement),
        "rm": (release.show_rm_feedback, release.show_rm_scope_of_improvement),
        "skip": (release.show_skip_feedback, release.show_skip_scope_of_improvement),
        "peer": (release.show_peer_feedback, release.show_peer_scope_of_improvement)
    }
    feedback_sections = []
    for submission in submissions:
        role = submission.evaluator_type
        if role not in visibility:
            continue
        show_feedback, show_scope = visibility[role]
        if not show_feedback:
            continue
        feedback_sections.append({
            "role": role,
            "evaluator_name": _evaluator_name(submission, questionnaire),
            "submitted_at": submission.submitted_at,
            "feedback": submission.feedback,
            "scope_of_improvement": submission.scope_of_improvement if show_scope else None
        })
    return {
        "employee_name": questionnaire.employee.full_name,
        "employee_number": questionnaire.employee.employee_number,
        "review_cycle": questionnaire.review_cycle.name,
        "performance_score": {
            "system_final_score": score.system_final_score if score else None,
            "hr_override_score": score.hr_override_score if score else None,
            "final_effective_score": score.final_effective_score if score else None
        } if release.show_performance_score else None,
        "ppi": {
            "current_cycle_score": ppi.current_cycle_score if ppi else None,
            "previous_cycle_score_1": ppi.previous_cycle_score_1 if ppi else None,
            "previous_cycle_score_2": ppi.previous_cycle_score_2 if ppi else None,
            "ppi_score": ppi.ppi_score if ppi else None
        } if release.show_ppi else None,
        "potential": {
            "rm_score": potential.rm_score if potential else None,
            "skip_score": potential.skip_score if potential else None,
            "final_potential_score": potential.final_potential_score if potential else None
        } if release.show_potential_score else None,
        "nine_box": {
            "box_label": nine_box.box_label if nine_box else None,
            "box_description": nine_box.box_description if nine_box else None,
            "performance_bucket": nine_box.performance_bucket if nine_box else None,
            "potential_bucket": nine_box.potential_bucket if nine_box else None,
            "ppi_score": nine_box.ppi_score if nine_box else None,
            "potential_score": nine_box.potential_score if nine_box else None
        } if release.show_nine_box else None,
        "feedback_sections": feedback_sections,
        "release_metadata": {
            "is_released_to_employee": release.is_released_to_employee,
            "released_at": release.released_at
        }
    }