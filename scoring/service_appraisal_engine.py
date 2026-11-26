from math import isfinite
from django.db import transaction
from django.utils import timezone
from rest_framework.exceptions import ValidationError
from .models import AppraisalRecommendationConfig, EmployeeAppraisalRecommendation, EmployeeNineBoxPlacement
from .scoring_locks import lock_questionnaire
from .score_readiness import get_ninebox_readiness, same_number, valid_number
from .result_invalidation import revoke_analytics_release

def _number(value, label):
    if value is None or isinstance(value, bool):
        raise ValidationError(f"{label} must be numeric.")
    try:
        value = float(value)
    except (TypeError, ValueError, OverflowError):
        raise ValidationError(f"{label} must be numeric.")
    if not isfinite(value):
        raise ValidationError(f"{label} must be finite.")
    return value

def _get_appraisal_configuration(box_label):
    config = AppraisalRecommendationConfig.objects.filter(box_label=box_label, is_active=True).first()
    if config is None:
        raise ValidationError(f"No active appraisal configuration found for '{box_label}'.")
    minimum = _number(config.min_percent, "Minimum appraisal percentage")
    maximum = _number(config.max_percent, "Maximum appraisal percentage")
    if minimum > maximum:
        raise ValidationError("Minimum appraisal percentage cannot exceed maximum appraisal percentage.")
    return minimum, maximum

def build_appraisal_recommendation(nine_box):
    minimum, maximum = _get_appraisal_configuration(nine_box.box_label)
    ppi = _number(nine_box.ppi_score, "PPI score")
    potential = _number(nine_box.potential_score, "Potential score")
    if not 0 <= ppi <= 10 or not 0 <= potential <= 10:
        raise ValidationError("PPI and Potential scores must be between 0 and 10.")
    position_factor = ((ppi / 10) + (potential / 10)) / 2
    suggested = round(minimum + ((maximum - minimum) * position_factor), 2)
    return {
        "nine_box_placement_id": nine_box.pk,
        "ppi_score_used": ppi,
        "potential_score_used": potential,
        "box_label": nine_box.box_label,
        "min_appraisal_percent": minimum,
        "max_appraisal_percent": maximum,
        "suggested_appraisal_percent": suggested
    }

def _recommendation_changed(existing, expected):
    if existing is None:
        return True
    for field, value in expected.items():
        previous = getattr(existing, field)
        if field in ("nine_box_placement_id", "box_label"):
            if previous != value:
                return True
        elif not same_number(previous, value, 1e-9):
            return True
    return False

def get_appraisal_readiness(employee_questionnaire, recommendation=None):
    from .score_readiness import get_appraisal_readiness as centralized_appraisal_readiness
    if recommendation is None:
        recommendation = get_appraisal_recommendation(employee_questionnaire)
    return centralized_appraisal_readiness(employee_questionnaire, recommendation=recommendation)

@transaction.atomic
def generate_appraisal_recommendation(employee_questionnaire, admin=None):
    questionnaire = lock_questionnaire(employee_questionnaire.pk)
    placement = EmployeeNineBoxPlacement.objects.filter(employee_questionnaire_id=questionnaire.pk).first()
    readiness = get_ninebox_readiness(questionnaire, placement=placement)
    if not readiness["is_current"]:
        raise ValidationError({
            "error": "Generate a current 9-box placement before generating an appraisal recommendation.",
            "missing_requirements": readiness["missing_requirements"]
        })
    expected = build_appraisal_recommendation(placement)
    existing = EmployeeAppraisalRecommendation.objects.select_for_update().filter(employee_questionnaire_id=questionnaire.pk).first()
    changed = _recommendation_changed(existing, expected)
    if existing is not None:
        changed = changed or existing.employee_id != questionnaire.employee_id or existing.review_cycle_id != questionnaire.review_cycle_id
        changed = changed or (existing.hr_override_percent is not None and not valid_number(existing.hr_override_percent))
    if not changed:
        effective = existing.hr_override_percent if existing.hr_override_percent is not None else existing.suggested_appraisal_percent
        if not same_number(existing.final_effective_appraisal_percent, effective, tolerance=1e-9):
            existing.final_effective_appraisal_percent = effective
            existing.save(update_fields=["final_effective_appraisal_percent", "calculated_at"])
            revoke_analytics_release(questionnaire.pk, admin)
        return existing
    recommendation, _ = EmployeeAppraisalRecommendation.objects.update_or_create(
        employee_questionnaire_id=questionnaire.pk,
        defaults={
            **expected,
            "employee_id": questionnaire.employee_id,
            "review_cycle_id": questionnaire.review_cycle_id,
            "hr_override_percent": None,
            "override_reason": None,
            "overridden_by": None,
            "overridden_at": None,
            "final_effective_appraisal_percent": expected["suggested_appraisal_percent"]
        }
    )
    revoke_analytics_release(questionnaire.pk, admin)
    return recommendation

@transaction.atomic
def apply_hr_override(recommendation, override_percent, reason, hr_employee):
    questionnaire = lock_questionnaire(recommendation.employee_questionnaire_id)
    recommendation = EmployeeAppraisalRecommendation.objects.select_for_update().filter(pk=recommendation.pk, employee_questionnaire_id=questionnaire.pk).first()
    if recommendation is None:
        raise ValidationError("Appraisal recommendation no longer exists.")
    readiness = get_appraisal_readiness(questionnaire, recommendation)
    if not readiness["is_current"]:
        raise ValidationError({
            "error": "Regenerate the appraisal recommendation before applying an override.",
            "missing_requirements": readiness["missing_requirements"]
        })
    override_percent = round(_number(override_percent, "HR override percentage"), 2)
    if not isinstance(reason, str) or not reason.strip():
        raise ValidationError("Override reason is required.")
    reason = reason.strip()
    override_changed = not same_number(recommendation.hr_override_percent, override_percent, tolerance=1e-9)
    reason_changed = (recommendation.override_reason or "") != reason
    effective_changed = not same_number(recommendation.final_effective_appraisal_percent, override_percent, tolerance=1e-9)
    if not override_changed and not reason_changed:
        return recommendation
    recommendation.hr_override_percent = override_percent
    recommendation.override_reason = reason
    recommendation.overridden_by = hr_employee
    recommendation.overridden_at = timezone.now()
    recommendation.final_effective_appraisal_percent = override_percent
    recommendation.save(update_fields=[
        "hr_override_percent", "override_reason", "overridden_by",
        "overridden_at", "final_effective_appraisal_percent"
    ])
    if effective_changed:
        revoke_analytics_release(questionnaire.pk, hr_employee)
    return recommendation

def get_appraisal_recommendation(employee_questionnaire):
    return EmployeeAppraisalRecommendation.objects.filter(employee_questionnaire_id=employee_questionnaire.pk).first()