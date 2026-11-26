from math import isfinite, isclose
from django.core.exceptions import ValidationError as DjangoValidationError
from rest_framework.exceptions import ValidationError
from .models import (
    EmployeePerformanceScore, EmployeePastPerformanceIndex,
    EmployeePotentialAssessment, EmployeePotentialScore,
    EmployeeNineBoxPlacement, EmployeeAppraisalRecommendation
)
from .services import get_active_config
from .services_potential import get_active_potential_config, calculate_potential_scores, _score_defaults, _score_changed
from .services_ppi import get_past_scores, build_ppi_values
from .services_ninebox import build_nine_box_result

_UNSET = object()

def same_number(first, second, tolerance=0.011):
    if first is None or second is None:
        return first is None and second is None
    try:
        first, second = float(first), float(second)
        return isfinite(first) and isfinite(second) and isclose(first, second, rel_tol=0, abs_tol=tolerance)
    except (TypeError, ValueError, OverflowError):
        return False

def valid_number(value):
    if value is None or isinstance(value, bool):
        return False
    try:
        return isfinite(float(value))
    except (TypeError, ValueError, OverflowError):
        return False

def valid_score(value):
    return valid_number(value) and 0 <= float(value) <= 10

def validation_message(exc):
    if isinstance(exc, DjangoValidationError):
        return "; ".join(exc.messages)
    if hasattr(exc, "detail"):
        return str(exc.detail)
    return str(exc)

def _unique_requirements(requirements):
    seen, unique = set(), []
    for requirement in requirements:
        key = str(requirement)
        if key not in seen:
            seen.add(key)
            unique.append(requirement)
    return unique

def readiness_result(record, missing=None, *, calculation_blockers=None, freshness_blockers=None):
    calculation_blockers = _unique_requirements(calculation_blockers or [])
    freshness_blockers = _unique_requirements([*(freshness_blockers or []), *(missing or [])])
    exists = record is not None
    can_calculate = not calculation_blockers
    is_current = exists and can_calculate and not freshness_blockers
    return {
        "exists": exists,
        "can_calculate": can_calculate,
        "is_current": is_current,
        "is_ready": is_current,
        "calculation_blockers": calculation_blockers,
        "freshness_blockers": freshness_blockers,
        "missing_requirements": _unique_requirements(calculation_blockers + freshness_blockers)
    }

def _performance_record(questionnaire):
    return EmployeePerformanceScore.objects.filter(employee_questionnaire_id=questionnaire.pk).first()

def _potential_record(questionnaire):
    return EmployeePotentialScore.objects.filter(employee_questionnaire_id=questionnaire.pk).first()

def _potential_assessment(questionnaire):
    return EmployeePotentialAssessment.objects.filter(employee_questionnaire_id=questionnaire.pk).first()

def _ppi_record(questionnaire):
    return EmployeePastPerformanceIndex.objects.filter(employee_id=questionnaire.employee_id, review_cycle_id=questionnaire.review_cycle_id).first()

def _ninebox_record(questionnaire):
    return EmployeeNineBoxPlacement.objects.filter(employee_questionnaire_id=questionnaire.pk).first()

def _appraisal_record(questionnaire):
    return EmployeeAppraisalRecommendation.objects.filter(employee_questionnaire_id=questionnaire.pk).first()

def _ownership_blockers(record, questionnaire, label, check_questionnaire=True):
    missing = []
    if check_questionnaire and record.employee_questionnaire_id != questionnaire.pk:
        missing.append(f"{label} belongs to another questionnaire.")
    if record.employee_id != questionnaire.employee_id or record.review_cycle_id != questionnaire.review_cycle_id:
        missing.append(f"{label} employee or review cycle information is inconsistent.")
    return missing

def _incomplete_calculation(result, message):
    if result.get("is_complete"):
        return []
    return [message, *result.get("missing_requirements", [])]

def _dependency_blockers(state, message):
    if state["is_current"]:
        return []
    return [message, *state["missing_requirements"]]

def get_performance_readiness(questionnaire, score=_UNSET):
    if score is _UNSET:
        score = _performance_record(questionnaire)
    calculation_blockers, freshness_blockers = [], []
    result = None
    if not get_active_config():
        calculation_blockers.append("No active Performance scoring configuration exists.")
    else:
        from .views import _calculation, _performance_values, _performance_values_changed
        try:
            result = _calculation(questionnaire)
            calculation_blockers.extend(_incomplete_calculation(result, "Performance evaluation or descriptive scoring is incomplete."))
        except (ValidationError, DjangoValidationError) as exc:
            calculation_blockers.append(validation_message(exc))
    if score is None:
        freshness_blockers.append("Performance score has not been calculated.")
    else:
        freshness_blockers.extend(_ownership_blockers(score, questionnaire, "Performance score"))
        if not valid_score(score.system_final_score):
            freshness_blockers.append("System Performance score is invalid.")
        if not valid_score(score.final_effective_score):
            freshness_blockers.append("Effective Performance score is invalid.")
        if score.hr_override_score is not None and not valid_score(score.hr_override_score):
            freshness_blockers.append("HR Performance override is invalid.")
            calculation_blockers.append("The existing HR Performance override must be corrected.")
        if result is not None and result.get("is_complete"):
            expected_values = _performance_values(questionnaire, result)
            if _performance_values_changed(score, expected_values):
                freshness_blockers.append("Performance score components or effective weights have changed. Recalculate the score.")
            expected_effective = score.hr_override_score if score.hr_override_score is not None else result["system_final_score"]
            if not same_number(score.final_effective_score, expected_effective, tolerance=1e-9):
                freshness_blockers.append("Effective Performance score does not match the current system score or HR override.")
    return readiness_result(score, calculation_blockers=calculation_blockers, freshness_blockers=freshness_blockers)

def get_potential_readiness(questionnaire, score=_UNSET, assessment=_UNSET):
    if assessment is _UNSET:
        assessment = _potential_assessment(questionnaire)
    if score is _UNSET:
        score = _potential_record(questionnaire)
    calculation_blockers, freshness_blockers = [], []
    result = None
    if assessment is None:
        calculation_blockers.append("Potential questionnaire has not been created.")
    else:
        calculation_blockers.extend(_ownership_blockers(assessment, questionnaire, "Potential assessment"))
        if not assessment.is_published:
            calculation_blockers.append("Potential questionnaire has not been published.")
        config = get_active_potential_config()
        if config is None:
            calculation_blockers.append("No active Potential scoring configuration exists.")
        elif not calculation_blockers:
            try:
                result = calculate_potential_scores(assessment, config=config)
                calculation_blockers.extend(_incomplete_calculation(result, "Potential evaluation or descriptive scoring is incomplete."))
            except (ValidationError, DjangoValidationError) as exc:
                calculation_blockers.append(validation_message(exc))
    if score is None:
        freshness_blockers.append("Potential score has not been calculated.")
    else:
        freshness_blockers.extend(_ownership_blockers(score, questionnaire, "Potential score"))
        if not valid_score(score.final_potential_score):
            freshness_blockers.append("Final Potential score is invalid.")
        if assessment is not None and result is not None and result.get("is_complete"):
            expected_values = _score_defaults(assessment, result)
            if _score_changed(score, expected_values):
                freshness_blockers.append("Potential score components or effective weights have changed. Recalculate the score.")
    return readiness_result(score, calculation_blockers=calculation_blockers, freshness_blockers=freshness_blockers)

def get_ppi_readiness(questionnaire, performance=_UNSET, ppi=_UNSET, performance_state=None):
    if performance is _UNSET:
        performance = _performance_record(questionnaire)
    if ppi is _UNSET:
        ppi = _ppi_record(questionnaire)
    if performance_state is None:
        performance_state = get_performance_readiness(questionnaire, score=performance)
    calculation_blockers = _dependency_blockers(performance_state, "A current Performance score is required before calculating PPI.")
    freshness_blockers, expected = [], None
    if not calculation_blockers and performance is not None:
        try:
            past_scores = get_past_scores(questionnaire.employee, questionnaire.review_cycle)
            expected = build_ppi_values(performance.final_effective_score, past_scores)
        except (ValidationError, DjangoValidationError) as exc:
            calculation_blockers.append(validation_message(exc))
    if ppi is None:
        freshness_blockers.append("PPI has not been calculated.")
    else:
        freshness_blockers.extend(_ownership_blockers(ppi, questionnaire, "PPI", check_questionnaire=False))
        if not valid_score(ppi.ppi_score):
            freshness_blockers.append("Calculated PPI score is invalid.")
        if expected is not None:
            field_messages = {
                "current_cycle_score": "Current-cycle Performance score has changed.",
                "previous_cycle_score_1": "Previous-cycle Performance score has changed.",
                "previous_cycle_score_2": "Second previous-cycle Performance score has changed.",
                "ppi_score": "Calculated PPI does not match its current Performance inputs."
            }
            for field, value in expected.items():
                if not same_number(getattr(ppi, field), value, tolerance=1e-9):
                    freshness_blockers.append(field_messages.get(field, f"PPI {field.replace('_', ' ')} is outdated."))
    return readiness_result(ppi, calculation_blockers=calculation_blockers, freshness_blockers=freshness_blockers)

def get_ninebox_readiness(questionnaire, ppi=_UNSET, potential=_UNSET, placement=_UNSET, ppi_state=None, potential_state=None):
    if ppi is _UNSET:
        ppi = _ppi_record(questionnaire)
    if potential is _UNSET:
        potential = _potential_record(questionnaire)
    if placement is _UNSET:
        placement = _ninebox_record(questionnaire)
    if ppi_state is None:
        ppi_state = get_ppi_readiness(questionnaire, ppi=ppi)
    if potential_state is None:
        potential_state = get_potential_readiness(questionnaire, score=potential)
    calculation_blockers = [
        *_dependency_blockers(ppi_state, "A current PPI score is required for Nine-Box placement."),
        *_dependency_blockers(potential_state, "A current Potential score is required for Nine-Box placement.")
    ]
    freshness_blockers, expected = [], None
    if not calculation_blockers and ppi is not None and potential is not None:
        try:
            expected = build_nine_box_result(ppi.ppi_score, potential.final_potential_score)
        except (ValidationError, DjangoValidationError) as exc:
            calculation_blockers.append(validation_message(exc))
    if placement is None:
        freshness_blockers.append("Nine-Box placement has not been generated.")
    else:
        freshness_blockers.extend(_ownership_blockers(placement, questionnaire, "Nine-Box placement"))
        if expected is not None:
            for field, value in expected.items():
                current = getattr(placement, field)
                matches = same_number(current, value, tolerance=1e-9) if field in ("ppi_score", "potential_score") else current == value
                if not matches:
                    freshness_blockers.append(f"Nine-Box {field.replace('_', ' ')} is outdated.")
    return readiness_result(placement, calculation_blockers=calculation_blockers, freshness_blockers=freshness_blockers)

def get_appraisal_readiness(questionnaire, recommendation=_UNSET, placement=_UNSET, ninebox_state=None):
    from .service_appraisal_engine import build_appraisal_recommendation, _recommendation_changed
    if recommendation is _UNSET:
        recommendation = _appraisal_record(questionnaire)
    if placement is _UNSET:
        placement = _ninebox_record(questionnaire)
    if ninebox_state is None:
        ninebox_state = get_ninebox_readiness(questionnaire, placement=placement)
    calculation_blockers = _dependency_blockers(ninebox_state, "A current Nine-Box placement is required for appraisal recommendation.")
    freshness_blockers, expected = [], None
    if not calculation_blockers and placement is not None:
        try:
            expected = build_appraisal_recommendation(placement)
        except (ValidationError, DjangoValidationError) as exc:
            calculation_blockers.append(validation_message(exc))
    if recommendation is None:
        freshness_blockers.append("Appraisal recommendation has not been generated.")
    else:
        freshness_blockers.extend(_ownership_blockers(recommendation, questionnaire, "Appraisal recommendation"))
        if expected is not None and _recommendation_changed(recommendation, expected):
            freshness_blockers.append("Appraisal recommendation is outdated.")
        if recommendation.hr_override_percent is not None and not valid_number(recommendation.hr_override_percent):
            freshness_blockers.append("HR appraisal override is invalid.")
        if not valid_number(recommendation.suggested_appraisal_percent):
            freshness_blockers.append("Suggested appraisal percentage is invalid.")
        effective = recommendation.hr_override_percent if recommendation.hr_override_percent is not None else recommendation.suggested_appraisal_percent
        if not valid_number(recommendation.final_effective_appraisal_percent) or not same_number(recommendation.final_effective_appraisal_percent, effective, tolerance=1e-9):
            freshness_blockers.append("Final effective appraisal percentage is inconsistent.")
    return readiness_result(recommendation, calculation_blockers=calculation_blockers, freshness_blockers=freshness_blockers)

def get_scoring_readiness(questionnaire):
    performance = _performance_record(questionnaire)
    assessment = _potential_assessment(questionnaire)
    potential = _potential_record(questionnaire)
    ppi = _ppi_record(questionnaire)
    placement = _ninebox_record(questionnaire)
    recommendation = _appraisal_record(questionnaire)
    performance_state = get_performance_readiness(questionnaire, score=performance)
    potential_state = get_potential_readiness(questionnaire, score=potential, assessment=assessment)
    ppi_state = get_ppi_readiness(questionnaire, performance=performance, ppi=ppi, performance_state=performance_state)
    ninebox_state = get_ninebox_readiness(questionnaire, ppi=ppi, potential=potential, placement=placement, ppi_state=ppi_state, potential_state=potential_state)
    appraisal_state = get_appraisal_readiness(questionnaire, recommendation=recommendation, placement=placement, ninebox_state=ninebox_state)
    core_states = {
        "performance": performance_state,
        "potential": potential_state,
        "ppi": ppi_state,
        "ninebox": ninebox_state
    }
    states = {**core_states, "appraisal": appraisal_state}
    core_missing = _unique_requirements([
        requirement for state in core_states.values()
        for requirement in state["missing_requirements"]
    ])
    all_missing = _unique_requirements([
        requirement for state in states.values()
        for requirement in state["missing_requirements"]
    ])
    core_current = all(state["is_current"] for state in core_states.values())
    all_current = all(state["is_current"] for state in states.values())
    return {
        **states,
        "questionnaire_id": questionnaire.pk,
        "assessment_id": assessment.pk if assessment else None,
        "is_current": core_current,
        "is_ready": core_current,
        "all_results_current": all_current,
        "all_results_ready": all_current,
        "performance_current": performance_state["is_current"],
        "potential_current": potential_state["is_current"],
        "ppi_current": ppi_state["is_current"],
        "ninebox_current": ninebox_state["is_current"],
        "appraisal_current": appraisal_state["is_current"],
        "missing_requirements": core_missing,
        "all_missing_requirements": all_missing
    }