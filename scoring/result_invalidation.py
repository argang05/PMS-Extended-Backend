from django.db import transaction
from django.utils import timezone
from .models import (
    EmployeePerformanceScore, EmployeePastPerformanceIndex,
    EmployeePotentialScore, EmployeeNineBoxPlacement,
    EmployeeAppraisalRecommendation, EmployeeAnalyticsRelease
)
from .scoring_locks import lock_questionnaire

def performance_dependency_state(questionnaire):
    questionnaire_id = questionnaire.pk
    return {
        "performance_score": EmployeePerformanceScore.objects.filter(employee_questionnaire_id=questionnaire_id).exists(),
        "ppi": EmployeePastPerformanceIndex.objects.filter(employee_id=questionnaire.employee_id, review_cycle_id=questionnaire.review_cycle_id).exists(),
        "ninebox": EmployeeNineBoxPlacement.objects.filter(employee_questionnaire_id=questionnaire_id).exists(),
        "appraisal": EmployeeAppraisalRecommendation.objects.filter(employee_questionnaire_id=questionnaire_id).exists(),
        "analytics_release": EmployeeAnalyticsRelease.objects.filter(employee_questionnaire_id=questionnaire_id, is_released_to_employee=True).exists()
    }

def potential_dependency_state(questionnaire_id):
    return {
        "potential_score": EmployeePotentialScore.objects.filter(employee_questionnaire_id=questionnaire_id).exists(),
        "ninebox": EmployeeNineBoxPlacement.objects.filter(employee_questionnaire_id=questionnaire_id).exists(),
        "appraisal": EmployeeAppraisalRecommendation.objects.filter(employee_questionnaire_id=questionnaire_id).exists(),
        "analytics_release": EmployeeAnalyticsRelease.objects.filter(employee_questionnaire_id=questionnaire_id, is_released_to_employee=True).exists(),
        "cached_potential": EmployeePerformanceScore.objects.filter(employee_questionnaire_id=questionnaire_id, potential_score__isnull=False).exists(),
        "cached_ninebox": EmployeePerformanceScore.objects.filter(employee_questionnaire_id=questionnaire_id, nine_box_label__isnull=False).exists()
    }

def empty_invalidation_result():
    return {"invalidated": False, "affected_results": []}

def revoke_analytics_release(questionnaire_id, admin=None):
    now = timezone.now()
    return bool(EmployeeAnalyticsRelease.objects.filter(
        employee_questionnaire_id=questionnaire_id, is_released_to_employee=True
    ).update(
        is_released_to_employee=False, released_at=None, released_by=None,
        unreleased_at=now, unreleased_by=admin, updated_at=now
    ))

def revoke_performance_release(questionnaire_id):
    return bool(EmployeePerformanceScore.objects.filter(
        employee_questionnaire_id=questionnaire_id, is_released_to_employee=True
    ).update(is_released_to_employee=False, released_at=None, released_by=None))

def _delete(queryset):
    count, _ = queryset.delete()
    return count > 0

def _result(previous_state, affected):
    return {
        "invalidated": bool(affected),
        "previous_state": previous_state,
        "affected_results": affected
    }

def _invalidate_common_dependencies(questionnaire_id, admin, affected):
    if _delete(EmployeeAppraisalRecommendation.objects.filter(employee_questionnaire_id=questionnaire_id)):
        affected.append("appraisal")
    if _delete(EmployeeNineBoxPlacement.objects.filter(employee_questionnaire_id=questionnaire_id)):
        affected.append("ninebox")
    if revoke_analytics_release(questionnaire_id, admin):
        affected.append("analytics_release")

@transaction.atomic
def invalidate_performance_dependencies(questionnaire, admin=None, preserve_score=False):
    questionnaire = lock_questionnaire(questionnaire.pk)
    questionnaire_id = questionnaire.pk
    previous_state = performance_dependency_state(questionnaire)
    affected = []
    _invalidate_common_dependencies(questionnaire_id, admin, affected)
    if _delete(EmployeePastPerformanceIndex.objects.filter(
        employee_id=questionnaire.employee_id, review_cycle_id=questionnaire.review_cycle_id
    )):
        affected.append("ppi")
    if preserve_score:
        if revoke_performance_release(questionnaire_id):
            affected.append("performance_release")
        EmployeePerformanceScore.objects.filter(employee_questionnaire_id=questionnaire_id).update(nine_box_label=None)
    elif _delete(EmployeePerformanceScore.objects.filter(employee_questionnaire_id=questionnaire_id)):
        affected.append("performance_score")
    return _result(previous_state, affected)

@transaction.atomic
def invalidate_potential_dependencies(assessment, admin=None, preserve_score=False):
    questionnaire = lock_questionnaire(assessment.employee_questionnaire_id)
    questionnaire_id = questionnaire.pk
    previous_state = potential_dependency_state(questionnaire_id)
    affected = []
    _invalidate_common_dependencies(questionnaire_id, admin, affected)
    if not preserve_score and _delete(EmployeePotentialScore.objects.filter(employee_questionnaire_id=questionnaire_id)):
        affected.append("potential_score")
    if previous_state["cached_potential"] or previous_state["cached_ninebox"]:
        EmployeePerformanceScore.objects.filter(employee_questionnaire_id=questionnaire_id).update(
            potential_score=None, nine_box_label=None
        )
        affected.append("performance_cached_analytics")
    return _result(previous_state, affected)

@transaction.atomic
def invalidate_ppi_dependencies(questionnaire, admin=None):
    questionnaire = lock_questionnaire(questionnaire.pk)
    questionnaire_id = questionnaire.pk
    affected = []
    _invalidate_common_dependencies(questionnaire_id, admin, affected)
    EmployeePerformanceScore.objects.filter(employee_questionnaire_id=questionnaire_id).update(nine_box_label=None)
    return {"invalidated": bool(affected), "affected_results": affected}

@transaction.atomic
def invalidate_ninebox_dependencies(questionnaire, admin=None):
    questionnaire = lock_questionnaire(questionnaire.pk)
    questionnaire_id = questionnaire.pk
    affected = []
    if _delete(EmployeeAppraisalRecommendation.objects.filter(employee_questionnaire_id=questionnaire_id)):
        affected.append("appraisal")
    if revoke_analytics_release(questionnaire_id, admin):
        affected.append("analytics_release")
    return {"invalidated": bool(affected), "affected_results": affected}