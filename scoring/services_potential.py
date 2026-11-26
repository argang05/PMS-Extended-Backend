from math import isfinite, isclose
from collections import defaultdict
from django.db import transaction
from django.utils import timezone
from rest_framework.exceptions import ValidationError
from .models import (
    PotentialScoreConfiguration, EmployeePotentialResponse,
    EmployeePotentialScore, EmployeePotentialDescriptiveAssessment,
    EmployeePotentialDescriptiveAssessmentHistory
)
from .scoring_locks import lock_potential_assessment
from .result_invalidation import invalidate_potential_dependencies, empty_invalidation_result
from .potential_evaluation.services import required_evaluators, evaluation_is_complete

def get_active_potential_config():
    return PotentialScoreConfiguration.objects.filter(is_active=True).order_by("-id").first()

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

def _same(first, second):
    if first is None or second is None:
        return first is None and second is None
    try:
        return isclose(float(first), float(second), rel_tol=0, abs_tol=1e-9)
    except (TypeError, ValueError, OverflowError):
        return False

def _weighted(entries):
    total_weight = sum(weight for _, weight in entries)
    if total_weight <= 0:
        return None
    return sum(score * weight for score, weight in entries) / total_weight

def _validate_config(config):
    groups = (
        (config.rm_weight, config.skip_weight),
        (config.objective_component_weight, config.descriptive_component_weight)
    )
    for group in groups:
        values = [_number(value, "Configuration weight") for value in group]
        if any(not 0 <= value <= 1 for value in values) or abs(sum(values) - 1) > 1e-6:
            raise ValidationError("Potential configuration weights must be between 0 and 1 and each group must total 1.")

def _skip_required(assessment):
    return "skip" in required_evaluators(assessment)

def _required_evaluators(assessment):
    return required_evaluators(assessment)

def _validate_assessment(assessment):
    if not assessment.is_published:
        raise ValidationError("The Potential questionnaire has not been published.")
    if not assessment.rm_submitted:
        raise ValidationError("RM potential questionnaire must be submitted first.")
    if _skip_required(assessment) and not assessment.skip_submitted:
        raise ValidationError("Skip potential questionnaire must be submitted first.")
    if not evaluation_is_complete(assessment):
        raise ValidationError("The Potential assessment has not been completed.")
    questionnaire = assessment.employee_questionnaire
    if questionnaire.employee_id != assessment.employee_id or questionnaire.review_cycle_id != assessment.review_cycle_id:
        raise ValidationError("Potential assessment employee or review cycle information is inconsistent.")

def _effective_evaluator_weights(assessment, config):
    if not _skip_required(assessment):
        return {"rm": 1.0, "skip": 0.0}
    return {
        "rm": _number(config.rm_weight, "RM weight"),
        "skip": _number(config.skip_weight, "Skip weight")
    }

def calculate_weighted_potential_score(responses):
    entries = []
    for response in responses:
        if response.is_not_applicable:
            continue
        item = response.questionnaire_item
        weight = _number(item.weightage if item else response.parameter.weightage, "Question weight")
        maximum = _number(item.effective_max_marks if item else response.parameter.max_marks, "Maximum marks")
        score = _number(response.score, "Response score")
        if weight <= 0 or maximum <= 0 or not 0 <= score <= maximum:
            raise ValidationError("Potential response contains an invalid score, weightage or maximum marks.")
        entries.append((score * 10 / maximum, weight))
    result = _weighted(entries)
    return round(result, 2) if result is not None else None

def _get_item_map(assessment):
    items = list(assessment.items.select_related("parameter", "parameter__category").order_by("order", "id"))
    item_map = {}
    for item in items:
        component = item.effective_question_type
        maximum = _number(item.effective_max_marks, "Maximum marks")
        weight = _number(item.weightage, "Question weight")
        if component not in ("objective", "descriptive") or maximum <= 0 or weight <= 0:
            raise ValidationError(f"Invalid Potential questionnaire configuration on item {item.pk}.")
        item_map[item.pk] = {
            "item": item, "component": component,
            "maximum": maximum, "weight": weight
        }
    return item_map

def _get_responses(assessment):
    return list(EmployeePotentialResponse.objects.filter(
        assessment_id=assessment.pk
    ).select_related(
        "parameter", "questionnaire_item", "admin_descriptive_score"
    ).order_by("evaluator_type", "id"))

def _build_response_map(assessment, responses, item_map):
    response_map = defaultdict(dict)
    required = set(_required_evaluators(assessment))
    missing = []
    for response in responses:
        if response.evaluator_type not in required:
            missing.append({"response_id": response.pk, "reason": "Unexpected evaluator response"})
            continue
        item_id = response.questionnaire_item_id
        if item_id is None:
            missing.append({"response_id": response.pk, "reason": "Unlinked historical response requires reconciliation"})
            continue
        if item_id not in item_map:
            missing.append({"response_id": response.pk, "reason": "Response belongs to a foreign questionnaire item"})
            continue
        if response.parameter_id != item_map[item_id]["item"].parameter_id:
            missing.append({"response_id": response.pk, "reason": "Question parameter mismatch"})
            continue
        expected_manager_id = assessment.reporting_manager_id if response.evaluator_type == "rm" else assessment.skip_level_manager_id
        if response.submitted_by_id and (
            response.submitted_by_id == assessment.employee_id
            or (expected_manager_id and response.submitted_by_id != expected_manager_id)
        ):
            missing.append({"response_id": response.pk, "reason": "Invalid evaluator identity"})
            continue
        if response.evaluator_type in response_map[item_id]:
            missing.append({
                "questionnaire_item_id": item_id, "evaluator": response.evaluator_type,
                "reason": "Duplicate evaluator responses"
            })
            continue
        response_map[item_id][response.evaluator_type] = response
    return response_map, missing

def _objective_response_score(response, maximum):
    if response.is_not_applicable:
        raise ValidationError("N/A is not supported in Potential evaluation.")
    if response.score is None:
        raise ValidationError("Objective response score is missing.")
    if (response.descriptive_answer or "").strip():
        raise ValidationError("Objective response contains an unexpected descriptive answer.")
    score = _number(response.score, "Objective score")
    if not 0 <= score <= maximum:
        raise ValidationError(f"Objective score must be between 0 and {maximum}.")
    return score * 10 / maximum

def _validate_descriptive_response(response):
    if response.is_not_applicable:
        raise ValidationError("N/A is not supported in Potential evaluation.")
    if response.score is not None:
        raise ValidationError("Descriptive responses cannot contain evaluator-assigned numerical scores.")
    if not (response.descriptive_answer or "").strip():
        raise ValidationError("Descriptive answer is missing.")

def _consolidated_descriptive_score(responses, maximum):
    marks = []
    for response in responses:
        mark = getattr(response, "admin_descriptive_score", None)
        if mark is None:
            raise ValidationError("Admin descriptive score is pending.")
        score = _number(mark.score, "Admin descriptive score")
        if not 1 <= score <= maximum:
            raise ValidationError(f"Admin descriptive score must be between 1 and {maximum}.")
        marks.append((score, mark.remarks or ""))
    if not marks:
        raise ValidationError("No descriptive marks are available.")
    if len(set(marks)) != 1:
        raise ValidationError("Consolidated descriptive marks are inconsistent across evaluators.")
    return marks[0][0] * 10 / maximum

def calculate_potential_scores(assessment, config=None, preference=None):
    _validate_assessment(assessment)
    config = config or get_active_potential_config()
    if config is None:
        raise ValidationError("No active potential score configuration found.")
    _validate_config(config)
    if preference is None:
        preference = EmployeePotentialDescriptiveAssessment.objects.filter(assessment_id=assessment.pk).first()
    descriptive_enabled = bool(preference and preference.is_enabled)
    item_map = _get_item_map(assessment)
    if not item_map:
        raise ValidationError("The Potential assessment contains no questionnaire items. Historical data may require reconciliation.")
    responses = _get_responses(assessment)
    response_map, missing = _build_response_map(assessment, responses, item_map)
    evaluators = _required_evaluators(assessment)
    evaluator_weights = _effective_evaluator_weights(assessment, config)
    objective_entries = defaultdict(list)
    descriptive_entries = []
    objective_count = sum(data["component"] == "objective" for data in item_map.values())
    descriptive_count = sum(data["component"] == "descriptive" for data in item_map.values())
    descriptive_applied = descriptive_enabled and descriptive_count > 0
    for item_id, data in item_map.items():
        component, weight, maximum = data["component"], data["weight"], data["maximum"]
        item_responses = response_map.get(item_id, {})
        valid_responses = []
        for evaluator in evaluators:
            response = item_responses.get(evaluator)
            if response is None:
                missing.append({
                    "questionnaire_item_id": item_id, "evaluator": evaluator,
                    "component": component, "reason": "Required response missing"
                })
                continue
            try:
                if component == "objective":
                    score = _objective_response_score(response, maximum)
                    objective_entries[evaluator].append((score, weight))
                else:
                    _validate_descriptive_response(response)
                    valid_responses.append(response)
            except ValidationError as exc:
                missing.append({
                    "questionnaire_item_id": item_id, "evaluator": evaluator,
                    "component": component, "reason": str(exc.detail)
                })
        if component == "descriptive" and descriptive_applied and len(valid_responses) == len(evaluators):
            try:
                score = _consolidated_descriptive_score(valid_responses, min(10, maximum))
                descriptive_entries.append((score, weight))
            except ValidationError as exc:
                missing.append({
                    "questionnaire_item_id": item_id, "component": "descriptive",
                    "reason": str(exc.detail)
                })
    rm_score = _weighted(objective_entries["rm"])
    skip_score = _weighted(objective_entries["skip"]) if "skip" in evaluators else None
    evaluator_scores = {"rm": rm_score, "skip": skip_score}
    objective_score = None
    if objective_count:
        if any(evaluator_scores[evaluator] is None for evaluator in evaluators):
            missing.append({"component": "objective", "reason": "Objective score missing for a required evaluator"})
        else:
            objective_score = sum(evaluator_scores[evaluator] * evaluator_weights[evaluator] for evaluator in evaluators)
    descriptive_score = _weighted(descriptive_entries) if descriptive_applied else None
    if descriptive_applied and descriptive_score is None:
        missing.append({"component": "descriptive", "reason": "Consolidated descriptive score is unavailable"})
    if objective_count and descriptive_applied:
        objective_weight = float(config.objective_component_weight)
        descriptive_weight = float(config.descriptive_component_weight)
    elif objective_count:
        objective_weight, descriptive_weight = 1.0, 0.0
    elif descriptive_applied:
        objective_weight, descriptive_weight = 0.0, 1.0
    else:
        objective_weight, descriptive_weight = 0.0, 0.0
        missing.append({"reason": "No enabled scoring component exists in this assessment"})
    final_score = None
    if not missing:
        final_score = (objective_score or 0) * objective_weight + (descriptive_score or 0) * descriptive_weight
        if not isfinite(final_score) or final_score < -1e-9 or final_score > 10 + 1e-9:
            raise ValidationError("Calculated Potential score is outside the valid 0–10 range.")
        final_score = min(10.0, max(0.0, final_score))
    return {
        "is_complete": not missing and final_score is not None,
        "missing_requirements": missing,
        "rm_score": round(rm_score, 2) if rm_score is not None else None,
        "skip_score": round(skip_score, 2) if skip_score is not None else None,
        "effective_rm_weight": evaluator_weights["rm"],
        "effective_skip_weight": evaluator_weights["skip"],
        "objective_final_score": round(objective_score, 2) if objective_score is not None else None,
        "descriptive_final_score": round(descriptive_score, 2) if descriptive_score is not None else None,
        "descriptive_scoring_applied": descriptive_applied,
        "effective_objective_component_weight": objective_weight,
        "effective_descriptive_component_weight": descriptive_weight,
        "final_potential_score": round(final_score, 2) if final_score is not None else None
    }

def _score_defaults(assessment, result):
    return {
        "employee_id": assessment.employee_id,
        "review_cycle_id": assessment.review_cycle_id,
        "rm_score": result["rm_score"],
        "skip_score": result["skip_score"],
        "effective_rm_weight": result["effective_rm_weight"],
        "effective_skip_weight": result["effective_skip_weight"],
        "objective_final_score": result["objective_final_score"],
        "descriptive_final_score": result["descriptive_final_score"],
        "descriptive_scoring_applied": result["descriptive_scoring_applied"],
        "effective_objective_component_weight": result["effective_objective_component_weight"],
        "effective_descriptive_component_weight": result["effective_descriptive_component_weight"],
        "final_potential_score": result["final_potential_score"]
    }

def _score_changed(existing, defaults):
    if existing is None:
        return True
    for field, value in defaults.items():
        previous = getattr(existing, field)
        if isinstance(value, bool) or field.endswith("_id"):
            if previous != value:
                return True
        elif not _same(previous, value):
            return True
    return False

def _finalize_descriptive_assessment(preference, result, admin):
    applied = result["descriptive_scoring_applied"]
    new_status = "finalized" if applied else "draft"
    new_score = result["descriptive_final_score"] if applied else None
    objective_weight = result["effective_objective_component_weight"] if applied else None
    descriptive_weight = result["effective_descriptive_component_weight"] if applied else None
    changed = (
        preference.status != new_status
        or not _same(preference.descriptive_score, new_score)
        or not _same(preference.objective_weight_used, objective_weight)
        or not _same(preference.descriptive_weight_used, descriptive_weight)
    )
    if not changed:
        return
    previous_status, previous_score = preference.status, preference.descriptive_score
    preference.status = new_status
    preference.descriptive_score = new_score
    preference.objective_weight_used = objective_weight
    preference.descriptive_weight_used = descriptive_weight
    preference.assessed_by = admin
    preference.assessed_at = timezone.now()
    preference.save(update_fields=[
        "status", "descriptive_score", "objective_weight_used",
        "descriptive_weight_used", "assessed_by", "assessed_at", "updated_at"
    ])
    EmployeePotentialDescriptiveAssessmentHistory.objects.create(
        assessment=preference, action="score_calculated",
        previous_status=previous_status, new_status=new_status,
        previous_score=previous_score, new_score=new_score,
        change_details={
            "descriptive_scoring_applied": applied,
            "objective_component_weight": objective_weight,
            "descriptive_component_weight": descriptive_weight,
            "final_potential_score": result["final_potential_score"]
        }, changed_by=admin
    )

@transaction.atomic
def save_potential_score(assessment_id, admin):
    questionnaire, assessment = lock_potential_assessment(assessment_id)
    preference, _ = EmployeePotentialDescriptiveAssessment.objects.get_or_create(
        assessment_id=assessment.pk, defaults={"is_enabled": False, "status": "draft"}
    )
    result = calculate_potential_scores(assessment, preference=preference)
    if not result["is_complete"]:
        raise ValidationError({
            "error": "Potential scoring is incomplete.",
            "missing_requirements": result["missing_requirements"]
        })
    existing = EmployeePotentialScore.objects.select_for_update().filter(
        employee_questionnaire_id=questionnaire.pk
    ).first()
    defaults = _score_defaults(assessment, result)
    changed = _score_changed(existing, defaults)
    invalidation = empty_invalidation_result()
    if changed:
        invalidation = invalidate_potential_dependencies(assessment, admin=admin, preserve_score=True)
        score, _ = EmployeePotentialScore.objects.update_or_create(
            employee_questionnaire_id=questionnaire.pk, defaults=defaults
        )
    else:
        score = existing
    _finalize_descriptive_assessment(preference, result, admin)
    return assessment, score, changed, invalidation

def serialize_potential_score(assessment, score):
    return {
        "assessment_id": assessment.pk,
        "questionnaire_id": assessment.employee_questionnaire_id,
        "employee_id": assessment.employee_id,
        "employee_name": assessment.employee.full_name,
        "employee_number": assessment.employee.employee_number,
        "review_cycle": assessment.review_cycle.name if assessment.review_cycle else None,
        "rm_score": score.rm_score,
        "skip_score": score.skip_score,
        "effective_rm_weight": score.effective_rm_weight,
        "effective_skip_weight": score.effective_skip_weight,
        "objective_final_score": score.objective_final_score,
        "descriptive_final_score": score.descriptive_final_score,
        "descriptive_scoring_applied": score.descriptive_scoring_applied,
        "effective_objective_component_weight": score.effective_objective_component_weight,
        "effective_descriptive_component_weight": score.effective_descriptive_component_weight,
        "final_potential_score": score.final_potential_score,
        "calculated_at": score.calculated_at
    }