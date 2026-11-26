from django.db import transaction
from django.utils import timezone
from django.core.exceptions import ValidationError
from rest_framework.exceptions import APIException
from evaluations.models import EmployeeQuestionnaireItem
from .models import (
    EmployeeDescriptiveAssessment, EmployeeDescriptiveAssessmentHistory,
    EmployeePerformanceScore
)
from .scoring_locks import lock_questionnaire
from .result_invalidation import (
    performance_dependency_state, invalidate_performance_dependencies,
    empty_invalidation_result
)

class PerformanceInvalidationConfirmationRequired(APIException):
    status_code = 409
    default_code = "invalidation_confirmation_required"
    default_detail = "Existing Performance-dependent results will be invalidated."

def is_descriptive_scoring_enabled(questionnaire):
    assessment = EmployeeDescriptiveAssessment.objects.filter(
        employee_questionnaire_id=questionnaire.pk
    ).values_list("is_enabled", flat=True).first()
    if assessment is not None:
        return assessment
    return EmployeePerformanceScore.objects.filter(
        employee_questionnaire_id=questionnaire.pk, descriptive_scoring_applied=True
    ).exists()

def has_descriptive_questions(questionnaire):
    items = EmployeeQuestionnaireItem.objects.filter(
        employee_questionnaire_id=questionnaire.pk
    ).select_related("parameter")
    return any(
        (item.question_type_snapshot or item.parameter.question_type) == "descriptive"
        for item in items
    )

def get_descriptive_preference(questionnaire):
    from .services import get_active_config
    config = get_active_config()
    enabled = is_descriptive_scoring_enabled(questionnaire)
    has_descriptive = has_descriptive_questions(questionnaire)
    dependencies = performance_dependency_state(questionnaire)
    applies = enabled and has_descriptive and config is not None
    return {
        "questionnaire_id": questionnaire.pk,
        "descriptive_scoring_enabled": enabled,
        "has_descriptive_questions": has_descriptive,
        "objective_component_weight": config.objective_component_weight if config else None,
        "descriptive_component_weight": config.descriptive_component_weight if config else None,
        "effective_objective_component_weight": config.objective_component_weight if applies else 1.0,
        "effective_descriptive_component_weight": config.descriptive_component_weight if applies else 0.0,
        "has_dependent_results": any(dependencies.values()),
        "dependent_results": dependencies
    }

def invalidate_performance_results(questionnaire, admin):
    return invalidate_performance_dependencies(questionnaire, admin)

def require_invalidation_confirmation(questionnaire, confirmed):
    dependencies = performance_dependency_state(questionnaire)
    if any(dependencies.values()) and not confirmed:
        raise PerformanceInvalidationConfirmationRequired({
            "code": "invalidation_confirmation_required",
            "message": "This change will invalidate existing Performance-dependent results.",
            "dependent_results": dependencies
        })

def reset_descriptive_calculation(questionnaire, admin):
    assessment = EmployeeDescriptiveAssessment.objects.filter(
        employee_questionnaire_id=questionnaire.pk
    ).first()
    if assessment is None:
        return
    previous_status, previous_score = assessment.status, assessment.descriptive_score
    if (
        assessment.status == "draft" and assessment.descriptive_score is None
        and assessment.objective_weight_used is None
        and assessment.descriptive_weight_used is None
    ):
        return
    assessment.status = "draft"
    assessment.descriptive_score = None
    assessment.objective_weight_used = None
    assessment.descriptive_weight_used = None
    assessment.assessed_by = admin
    assessment.assessed_at = timezone.now()
    assessment.save(update_fields=[
        "status", "descriptive_score", "objective_weight_used",
        "descriptive_weight_used", "assessed_by", "assessed_at", "updated_at"
    ])
    EmployeeDescriptiveAssessmentHistory.objects.create(
        assessment=assessment, action="descriptive_scores_changed",
        previous_status=previous_status, new_status=assessment.status,
        previous_score=previous_score, new_score=None,
        change_details={}, changed_by=admin
    )

@transaction.atomic
def set_descriptive_preference(questionnaire, enabled, admin, confirm_invalidation=False):
    if not isinstance(enabled, bool):
        raise ValidationError("Descriptive scoring preference must be a boolean.")
    questionnaire = lock_questionnaire(questionnaire.pk)
    previous_enabled = is_descriptive_scoring_enabled(questionnaire)
    if previous_enabled == enabled:
        return {
            **get_descriptive_preference(questionnaire),
            "changed": False, "recalculation_required": False,
            "invalidation": empty_invalidation_result()
        }
    if enabled and not has_descriptive_questions(questionnaire):
        raise ValidationError("This questionnaire does not contain descriptive questions.")
    require_invalidation_confirmation(questionnaire, confirm_invalidation)
    assessment, _ = EmployeeDescriptiveAssessment.objects.select_for_update().get_or_create(
        employee_questionnaire=questionnaire, defaults={"is_enabled": previous_enabled}
    )
    previous_status, previous_score = assessment.status, assessment.descriptive_score
    assessment.is_enabled = enabled
    assessment.status = "draft"
    assessment.descriptive_score = None
    assessment.objective_weight_used = None
    assessment.descriptive_weight_used = None
    assessment.assessed_by = admin
    assessment.assessed_at = timezone.now()
    assessment.save(update_fields=[
        "is_enabled", "status", "descriptive_score", "objective_weight_used",
        "descriptive_weight_used", "assessed_by", "assessed_at", "updated_at"
    ])
    invalidation = invalidate_performance_results(questionnaire, admin)
    EmployeeDescriptiveAssessmentHistory.objects.create(
        assessment=assessment, action="scoring_preference_changed",
        previous_status=previous_status, new_status=assessment.status,
        previous_score=previous_score, new_score=None,
        change_details={
            "previous_enabled": previous_enabled, "new_enabled": enabled,
            "invalidated_results": invalidation["affected_results"]
        }, changed_by=admin
    )
    return {
        **get_descriptive_preference(questionnaire),
        "changed": True, "recalculation_required": True,
        "invalidation": invalidation
    }