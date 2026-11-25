from math import isclose
from django.db import transaction
from django.utils import timezone
from django.core.exceptions import ValidationError
from evaluations.models import (
    EmployeeQuestionnaireStageSubmission,
    EmployeeQuestionnairePeerAssignment
)
from .models import (
    EmployeeAnalyticsRelease, EmployeePerformanceScore,
    EmployeePastPerformanceIndex, EmployeePotentialScore,
    EmployeeNineBoxPlacement
)
from .services import get_active_config, calculate_questionnaire_scores


def get_or_create_analytics_release(questionnaire):
    release, _ = EmployeeAnalyticsRelease.objects.get_or_create(
        employee_questionnaire=questionnaire
    )
    return release


def _same(a, b):
    if a is None or b is None:
        return a is None and b is None
    return isclose(float(a), float(b), rel_tol=0, abs_tol=0.011)


def _skip_required(questionnaire):
    manager = questionnaire.employee.reporting_manager
    return not str(getattr(manager, "band", "") or "").upper().startswith("SM")


def _peer_status(questionnaire):
    peer_ids = set(
        EmployeeQuestionnairePeerAssignment.objects.filter(
            employee_questionnaire=questionnaire
        ).values_list("peer_id", flat=True)
    )

    if not peer_ids and questionnaire.peer_reviewer_id:
        peer_ids.add(questionnaire.peer_reviewer_id)

    submitted = set(
        EmployeeQuestionnaireStageSubmission.objects.filter(
            employee_questionnaire=questionnaire,
            evaluator_type="peer",
            status="submitted",
            submitted_by_id__in=peer_ids
        ).values_list("submitted_by_id", flat=True)
    )

    return {
        "assigned_count": len(peer_ids),
        "submitted_count": len(submitted),
        "is_complete": bool(peer_ids) and submitted == peer_ids
    }


def _submission_status(questionnaire):
    types = set(
        EmployeeQuestionnaireStageSubmission.objects.filter(
            employee_questionnaire=questionnaire,
            status="submitted"
        ).values_list("evaluator_type", flat=True)
    )

    return {
        "self": "self" in types,
        "rm": "rm" in types,
        "skip": "skip" in types,
        "peer": "peer" in types
    }


def get_analytics_readiness(questionnaire):
    questionnaire = type(questionnaire).objects.select_related(
        "employee", "employee__reporting_manager",
        "review_cycle", "peer_reviewer"
    ).get(pk=questionnaire.pk)

    missing = []

    score = EmployeePerformanceScore.objects.filter(
        employee_questionnaire=questionnaire
    ).first()

    ppi = EmployeePastPerformanceIndex.objects.filter(
        employee=questionnaire.employee,
        review_cycle=questionnaire.review_cycle
    ).first()

    potential = EmployeePotentialScore.objects.filter(
        employee_questionnaire=questionnaire
    ).first()

    nine_box = EmployeeNineBoxPlacement.objects.filter(
        employee_questionnaire=questionnaire
    ).first()

    stages = _submission_status(questionnaire)
    peers = _peer_status(questionnaire)
    skip_required = _skip_required(questionnaire)

    if not stages["self"]:
        missing.append("Self evaluation has not been submitted.")

    if not stages["rm"]:
        missing.append("Reporting manager evaluation has not been submitted.")

    if skip_required and not stages["skip"]:
        missing.append("Skip-level evaluation has not been submitted.")

    if not peers["is_complete"]:
        missing.append(
            f"Peer evaluations incomplete: {peers['submitted_count']}/"
            f"{peers['assigned_count']} submitted."
        )

    has_score = bool(score and score.final_effective_score is not None)
    has_ppi = bool(ppi)
    has_potential = bool(potential and potential.final_potential_score is not None)
    has_nine_box = bool(nine_box)

    if not has_score:
        missing.append("Performance score is unavailable.")

    if not has_ppi:
        missing.append("PPI is unavailable.")

    if not has_potential:
        missing.append("Potential score is unavailable.")

    if not has_nine_box:
        missing.append("9-box placement is unavailable.")

    config = get_active_config()

    if not config:
        missing.append("Active scoring configuration is unavailable.")
    else:
        try:
            result = calculate_questionnaire_scores(questionnaire, config)

            if not result["is_complete"]:
                missing.append("Performance scoring is incomplete.")

            elif score:
                if not _same(score.system_final_score, result["system_final_score"]):
                    missing.append("Performance score is outdated. Recalculate it.")

                if score.descriptive_scoring_applied != result["descriptive_scoring_applied"]:
                    missing.append("Performance scoring mode has changed.")

                if not _same(score.objective_final_score, result["objective_final_score"]):
                    missing.append("Objective scoring component is outdated.")

                if not _same(score.descriptive_final_score, result["descriptive_final_score"]):
                    missing.append("Descriptive scoring component is outdated.")

        except (ValidationError, ValueError, TypeError) as exc:
            missing.append(f"Performance calculation validation failed: {exc}")

    if score:
        expected = (
            score.hr_override_score
            if score.hr_override_score is not None
            else score.system_final_score
        )

        if not _same(score.final_effective_score, expected):
            missing.append("Final effective performance score is inconsistent.")

        if ppi and not _same(ppi.current_cycle_score, score.final_effective_score):
            missing.append("PPI is outdated relative to the performance score.")

    if ppi and potential and nine_box:
        if not _same(nine_box.ppi_score, ppi.ppi_score):
            missing.append("9-box placement is outdated relative to PPI.")

        if not _same(nine_box.potential_score, potential.final_potential_score):
            missing.append("9-box placement is outdated relative to potential score.")

    return {
        "is_ready": not missing,
        "missing_requirements": missing,
        "has_processed_score": has_score,
        "has_ppi": has_ppi,
        "has_potential": has_potential,
        "has_nine_box": has_nine_box,
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
    release = get_or_create_analytics_release(questionnaire)
    readiness = get_analytics_readiness(questionnaire)

    if not readiness["is_ready"]:
        return {
            "success": False,
            "message": "Analytics data is not ready for release.",
            "missing_requirements": readiness["missing_requirements"]
        }

    release.is_released_to_employee = True
    release.released_at = timezone.now()
    release.released_by = hr_user
    release.save()

    return {
        "success": True,
        "message": "Analytics data released successfully.",
        "missing_requirements": []
    }


@transaction.atomic
def unrelease_analytics_to_employee(questionnaire, hr_user):
    release = get_or_create_analytics_release(questionnaire)
    release.is_released_to_employee = False
    release.unreleased_at = timezone.now()
    release.unreleased_by = hr_user
    release.save()

    return {
        "success": True,
        "message": "Analytics data unreleased successfully."
    }


def _evaluator_name(submission, questionnaire):
    if submission.evaluator_type == "self":
        return questionnaire.employee.full_name

    if submission.submitted_by:
        return (
            getattr(submission.submitted_by, "full_name", None)
            or str(submission.submitted_by)
        )

    return {
        "rm": "Reporting Manager",
        "skip": "Skip-Level Manager",
        "peer": "Peer Reviewer"
    }.get(submission.evaluator_type, submission.evaluator_type)


def build_employee_analytics_report(questionnaire):
    release = get_or_create_analytics_release(questionnaire)

    score = EmployeePerformanceScore.objects.filter(
        employee_questionnaire=questionnaire
    ).first()

    ppi = EmployeePastPerformanceIndex.objects.filter(
        employee=questionnaire.employee,
        review_cycle=questionnaire.review_cycle
    ).first()

    potential = EmployeePotentialScore.objects.filter(
        employee_questionnaire=questionnaire
    ).first()

    nine_box = EmployeeNineBoxPlacement.objects.filter(
        employee_questionnaire=questionnaire
    ).first()

    submissions = EmployeeQuestionnaireStageSubmission.objects.filter(
        employee_questionnaire=questionnaire,
        status="submitted"
    ).select_related("submitted_by").order_by("submitted_at", "id")

    visibility = {
        "self": (
            release.show_self_feedback,
            release.show_self_scope_of_improvement
        ),
        "rm": (
            release.show_rm_feedback,
            release.show_rm_scope_of_improvement
        ),
        "skip": (
            release.show_skip_feedback,
            release.show_skip_scope_of_improvement
        ),
        "peer": (
            release.show_peer_feedback,
            release.show_peer_scope_of_improvement
        )
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
            "scope_of_improvement": (
                submission.scope_of_improvement if show_scope else None
            )
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