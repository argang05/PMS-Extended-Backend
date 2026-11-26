from math import isfinite, isclose
from django.db import transaction
from rest_framework.exceptions import ValidationError
from evaluations.models import EmployeeQuestionnaire
from .models import EmployeePastPerformanceIndex, EmployeePerformanceScore
from .scoring_locks import lock_questionnaire
from .result_invalidation import invalidate_ppi_dependencies, empty_invalidation_result

def _number(value):
    if value is None or isinstance(value, bool):
        raise ValidationError("A valid performance score is required for PPI calculation.")
    try:
        value = float(value)
    except (TypeError, ValueError, OverflowError):
        raise ValidationError("A valid performance score is required for PPI calculation.")
    if not isfinite(value) or not 0 <= value <= 10:
        raise ValidationError("Performance scores must be between 0 and 10.")
    return value

def _same(first, second):
    if first is None or second is None:
        return first is None and second is None
    try:
        return isclose(float(first), float(second), rel_tol=0, abs_tol=1e-9)
    except (TypeError, ValueError, OverflowError):
        return False

def get_past_scores(employee, current_cycle):
    scores = EmployeePerformanceScore.objects.filter(
        employee=employee,
        review_cycle_id__lt=current_cycle.pk,
        final_effective_score__isnull=False
    ).order_by("-review_cycle_id", "-id").values_list("final_effective_score", flat=True)
    return list(scores)

def calculate_ppi_score(current_score, past_scores):
    current_score = _number(current_score)
    if not past_scores:
        return round(current_score, 2)
    previous_1 = _number(past_scores[0])
    if len(past_scores) == 1:
        return round(previous_1 * 0.4 + current_score * 0.6, 2)
    previous_2 = _number(past_scores[1])
    return round(previous_2 * 0.2 + previous_1 * 0.3 + current_score * 0.5, 2)

def build_ppi_values(current_score, past_scores):
    current_score = _number(current_score)
    previous_1 = _number(past_scores[0]) if len(past_scores) >= 1 else None
    previous_2 = _number(past_scores[1]) if len(past_scores) >= 2 else None
    return {
        "current_cycle_score": current_score,
        "previous_cycle_score_1": previous_1,
        "previous_cycle_score_2": previous_2,
        "ppi_score": calculate_ppi_score(current_score, past_scores)
    }

def ppi_values_changed(existing, values):
    if existing is None:
        return True
    return any(not _same(getattr(existing, field), value) for field, value in values.items())

def is_ppi_current(questionnaire, performance=None, ppi=None):
    if performance is None:
        performance = EmployeePerformanceScore.objects.filter(
            employee_questionnaire_id=questionnaire.pk
        ).first()
    if ppi is None:
        ppi = EmployeePastPerformanceIndex.objects.filter(
            employee_id=questionnaire.employee_id,
            review_cycle_id=questionnaire.review_cycle_id
        ).first()
    if performance is None or ppi is None or performance.final_effective_score is None:
        return False
    if performance.employee_id != questionnaire.employee_id or performance.review_cycle_id != questionnaire.review_cycle_id:
        return False
    if ppi.employee_id != questionnaire.employee_id or ppi.review_cycle_id != questionnaire.review_cycle_id:
        return False
    try:
        past_scores = get_past_scores(questionnaire.employee, questionnaire.review_cycle)
        values = build_ppi_values(performance.final_effective_score, past_scores)
    except ValidationError:
        return False
    return not ppi_values_changed(ppi, values)

@transaction.atomic
def save_ppi_with_state(employee, cycle, current_score, past_scores, admin=None):
    questionnaire_id = EmployeeQuestionnaire.objects.filter(
        employee_id=employee.pk, review_cycle_id=cycle.pk
    ).values_list("pk", flat=True).first()
    if questionnaire_id is None:
        raise ValidationError("Employee questionnaire not found for PPI calculation.")
    questionnaire = lock_questionnaire(questionnaire_id)
    performance = EmployeePerformanceScore.objects.filter(
        employee_questionnaire_id=questionnaire.pk
    ).first()
    if performance is None or performance.final_effective_score is None:
        raise ValidationError("Calculate the Performance score before calculating PPI.")
    if not _same(performance.final_effective_score, current_score):
        raise ValidationError("The Performance score has changed. Refresh before calculating PPI.")
    current_past_scores = get_past_scores(employee, cycle)
    values = build_ppi_values(current_score, current_past_scores)
    existing = EmployeePastPerformanceIndex.objects.select_for_update().filter(
        employee_id=employee.pk, review_cycle_id=cycle.pk
    ).first()
    changed = ppi_values_changed(existing, values)
    invalidation = empty_invalidation_result()
    if changed:
        invalidation = invalidate_ppi_dependencies(questionnaire, admin=admin)
        ppi, _ = EmployeePastPerformanceIndex.objects.update_or_create(
            employee_id=employee.pk, review_cycle_id=cycle.pk, defaults=values
        )
    else:
        ppi = existing
    return ppi, changed, invalidation

def save_ppi(employee, cycle, current_score, past_scores):
    ppi, _, _ = save_ppi_with_state(employee, cycle, current_score, past_scores)
    return ppi

def serialize_ppi(ppi):
    return {
        "employee_id": ppi.employee_id,
        "review_cycle_id": ppi.review_cycle_id,
        "current_cycle_score": ppi.current_cycle_score,
        "previous_cycle_score_1": ppi.previous_cycle_score_1,
        "previous_cycle_score_2": ppi.previous_cycle_score_2,
        "ppi_score": ppi.ppi_score
    }