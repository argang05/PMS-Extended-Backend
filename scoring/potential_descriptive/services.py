from django.db import transaction
from django.db.models import Prefetch
from django.utils import timezone
from django.core.exceptions import ValidationError as DjangoValidationError
from rest_framework.exceptions import ValidationError, NotFound, APIException
from scoring.models import (
    EmployeePotentialAssessment, EmployeePotentialQuestionnaireItem,
    EmployeePotentialResponse, EmployeePotentialDescriptiveScore,
    EmployeePotentialDescriptiveAssessment, EmployeePotentialDescriptiveAssessmentHistory,
    PotentialScoreConfiguration
)
from scoring.scoring_locks import lock_potential_assessment
from scoring.result_invalidation import potential_dependency_state, invalidate_potential_dependencies, empty_invalidation_result
from scoring.potential_evaluation.services import required_evaluators, evaluation_is_complete

class PotentialDescriptiveConflict(APIException):
    status_code = 409
    default_code = "potential_descriptive_conflict"
    default_detail = "The descriptive assessment has changed. Refresh before continuing."

class PotentialInvalidationConfirmationRequired(APIException):
    status_code = 409
    default_code = "potential_invalidation_confirmation_required"
    default_detail = "Existing Potential-dependent results will be invalidated."

def get_assessment(assessment_id, lock=False):
    if lock:
        _, assessment = lock_potential_assessment(assessment_id)
        return assessment
    try:
        return EmployeePotentialAssessment.objects.get(pk=assessment_id)
    except EmployeePotentialAssessment.DoesNotExist:
        raise NotFound("Potential assessment not found.")

def get_preference(assessment):
    preference, _ = EmployeePotentialDescriptiveAssessment.objects.get_or_create(
        assessment_id=assessment.pk, defaults={"is_enabled": False, "status": "draft"}
    )
    return preference

def get_configuration():
    return PotentialScoreConfiguration.objects.filter(is_active=True).order_by("-id").first()

def get_component_weights():
    config = get_configuration()
    return {
        "objective_component_weight": config.objective_component_weight if config else 0.8,
        "descriptive_component_weight": config.descriptive_component_weight if config else 0.2
    }

def get_descriptive_items(assessment):
    responses = EmployeePotentialResponse.objects.select_related(
        "submitted_by", "admin_descriptive_score", "admin_descriptive_score__scored_by"
    ).order_by("evaluator_type", "id")
    items = EmployeePotentialQuestionnaireItem.objects.filter(
        assessment_id=assessment.pk
    ).select_related("parameter", "parameter__category").prefetch_related(
        Prefetch("responses", queryset=responses, to_attr="loaded_responses")
    ).order_by("order", "id")
    return [item for item in items if item.effective_question_type == "descriptive"]

def get_submitted_evaluator_types(assessment):
    evaluators = set()
    if assessment.rm_submitted:
        evaluators.add("rm")
    if assessment.skip_submitted:
        evaluators.add("skip")
    return evaluators

def get_valid_responses(assessment, item):
    submitted_evaluators = get_submitted_evaluator_types(assessment)
    expected_evaluators = set(required_evaluators(assessment))
    manager_ids = {
        "rm": assessment.reporting_manager_id,
        "skip": assessment.skip_level_manager_id
    }
    responses = []
    for response in item.loaded_responses:
        evaluator = response.evaluator_type
        if evaluator not in submitted_evaluators or evaluator not in expected_evaluators:
            continue
        if response.assessment_id != assessment.pk or response.parameter_id != item.parameter_id:
            continue
        if response.is_not_applicable or response.score is not None:
            continue
        if not (response.descriptive_answer or "").strip():
            continue
        if response.submitted_by_id == assessment.employee_id:
            continue
        expected_manager_id = manager_ids[evaluator]
        if expected_manager_id and response.submitted_by_id not in (None, expected_manager_id):
            continue
        responses.append(response)
    return responses

def get_response_mark(response):
    return getattr(response, "admin_descriptive_score", None)

def get_consolidated_state(responses):
    marks = [get_response_mark(response) for response in responses]
    existing_marks = [mark for mark in marks if mark is not None]
    if not responses or not existing_marks:
        return {"score": None, "remarks": "", "is_scored": False, "has_conflict": False}
    score_values = {mark.score for mark in existing_marks}
    remark_values = {mark.remarks or "" for mark in existing_marks}
    consistent = (
        len(existing_marks) == len(responses)
        and len(score_values) == 1 and len(remark_values) == 1
    )
    if not consistent:
        return {"score": None, "remarks": "", "is_scored": False, "has_conflict": True}
    mark = existing_marks[0]
    return {
        "score": mark.score, "remarks": mark.remarks or "",
        "is_scored": True, "has_conflict": False
    }

def build_question_state(assessment, item):
    responses = get_valid_responses(assessment, item)
    consolidated = get_consolidated_state(responses)
    expected_evaluators = set(required_evaluators(assessment))
    evaluator_types = {response.evaluator_type for response in responses}
    maximum = min(10, item.effective_max_marks)
    complete = (
        evaluator_types == expected_evaluators
        and len(responses) == len(expected_evaluators)
        and len(item.loaded_responses) == len(expected_evaluators)
    )
    if consolidated["is_scored"] and not 1 <= consolidated["score"] <= maximum:
        consolidated["is_scored"] = False
        consolidated["has_conflict"] = True
    return {
        "questionnaire_item_id": item.pk,
        "parameter_id": item.parameter_id,
        "question_text": item.effective_question_text,
        "question_type": "descriptive",
        "category_name": item.category_name_snapshot,
        "weightage": item.weightage,
        "max_marks": maximum,
        "responses": responses,
        "responses_complete": complete,
        "can_score": complete and evaluation_is_complete(assessment),
        **consolidated
    }

def get_question_states(assessment):
    return [build_question_state(assessment, item) for item in get_descriptive_items(assessment)]

def get_unlinked_descriptive_response_count(assessment):
    return EmployeePotentialResponse.objects.filter(
        assessment_id=assessment.pk, questionnaire_item__isnull=True,
        parameter__question_type="descriptive"
    ).count()

def build_readiness(assessment, preference, question_states=None):
    states = question_states if question_states is not None else get_question_states(assessment)
    evaluation_complete = evaluation_is_complete(assessment)
    total_questions = len(states)
    scored_questions = sum(state["is_scored"] and state["responses_complete"] for state in states)
    incomplete_questions = sum(not state["responses_complete"] for state in states)
    conflicting_questions = sum(state["has_conflict"] for state in states)
    pending_questions = total_questions - scored_questions
    unlinked_responses = get_unlinked_descriptive_response_count(assessment)
    descriptive_ready = (
        not preference.is_enabled
        or (pending_questions == 0 and unlinked_responses == 0)
    )
    ready_for_calculation = False
    missing_requirements = []
    if evaluation_complete and descriptive_ready:
        from scoring.services_potential import calculate_potential_scores
        try:
            calculation = calculate_potential_scores(assessment, preference=preference)
            ready_for_calculation = calculation["is_complete"]
            missing_requirements = calculation["missing_requirements"]
        except ValidationError as exc:
            missing_requirements = [{"reason": str(exc.detail)}]
    if not evaluation_complete:
        readiness_status = "evaluation_pending"
    elif unlinked_responses:
        readiness_status = "legacy_reconciliation_required"
    elif not preference.is_enabled:
        readiness_status = "objective_only" if ready_for_calculation else "responses_incomplete"
    elif not total_questions:
        readiness_status = "no_descriptive_questions"
    elif incomplete_questions:
        readiness_status = "responses_incomplete"
    elif pending_questions:
        readiness_status = "descriptive_scoring_pending"
    elif not ready_for_calculation:
        readiness_status = "responses_incomplete"
    else:
        readiness_status = "ready"
    return {
        "is_enabled": preference.is_enabled,
        "evaluation_complete": evaluation_complete,
        "skip_required": "skip" in required_evaluators(assessment),
        "required_evaluators": list(required_evaluators(assessment)),
        "total_descriptive_questions": total_questions,
        "scored_questions": scored_questions,
        "pending_questions": pending_questions,
        "incomplete_questions": incomplete_questions,
        "conflicting_questions": conflicting_questions,
        "unlinked_descriptive_responses": unlinked_responses,
        "descriptive_ready": descriptive_ready,
        "ready_for_calculation": ready_for_calculation,
        "readiness_status": readiness_status,
        "missing_requirements": missing_requirements
    }

def serialize_employee(employee):
    if employee is None:
        return None
    return {
        "id": employee.pk,
        "employee_number": employee.employee_number,
        "full_name": f"{employee.first_name or ''} {employee.last_name or ''}".strip()
    }

def serialize_response(response):
    mark = get_response_mark(response)
    return {
        "id": response.pk,
        "evaluator_type": response.evaluator_type,
        "submitted_by": serialize_employee(response.submitted_by),
        "descriptive_answer": response.descriptive_answer,
        "submitted_at": response.submitted_at,
        "admin_score": mark.score if mark else None,
        "admin_remarks": mark.remarks if mark else "",
        "scored_by": serialize_employee(mark.scored_by) if mark else None,
        "scored_at": mark.scored_at if mark else None,
        "updated_at": mark.updated_at if mark else None
    }

def serialize_question_state(state):
    return {
        **{key: value for key, value in state.items() if key != "responses"},
        "responses": [serialize_response(response) for response in state["responses"]]
    }

def build_preference_payload(assessment, preference, states=None):
    states = states if states is not None else get_question_states(assessment)
    weights = get_component_weights()
    dependencies = potential_dependency_state(assessment.employee_questionnaire_id)
    descriptive_applies = preference.is_enabled and bool(states)
    has_objective = any(
        item.effective_question_type == "objective"
        for item in assessment.items.select_related("parameter")
    )
    if has_objective and descriptive_applies:
        objective_weight = weights["objective_component_weight"]
        descriptive_weight = weights["descriptive_component_weight"]
    elif descriptive_applies:
        objective_weight, descriptive_weight = 0.0, 1.0
    elif has_objective:
        objective_weight, descriptive_weight = 1.0, 0.0
    else:
        objective_weight, descriptive_weight = 0.0, 0.0
    return {
        "assessment_id": assessment.pk,
        "employee_questionnaire_id": assessment.employee_questionnaire_id,
        "is_enabled": preference.is_enabled,
        "status": preference.status,
        "revision": preference.updated_at.isoformat(),
        "descriptive_score": preference.descriptive_score,
        "assessed_at": preference.assessed_at,
        "assessed_by": serialize_employee(preference.assessed_by),
        "has_descriptive_questions": bool(states),
        **weights,
        "effective_objective_component_weight": objective_weight,
        "effective_descriptive_component_weight": descriptive_weight,
        "has_dependent_results": any(dependencies.values()),
        "dependent_results": dependencies,
        "readiness": build_readiness(assessment, preference, states)
    }

def build_workspace_payload(assessment, preference):
    states = get_question_states(assessment)
    employee = assessment.employee
    return {
        "assessment_id": assessment.pk,
        "employee_questionnaire_id": assessment.employee_questionnaire_id,
        "employee": {
            **serialize_employee(employee),
            "department": employee.department,
            "designation": employee.designation
        },
        "review_cycle_id": assessment.review_cycle_id,
        "review_cycle_name": assessment.review_cycle.name,
        "assessment_status": assessment.status,
        "is_published": assessment.is_published,
        "rm_submitted": assessment.rm_submitted,
        "skip_submitted": assessment.skip_submitted,
        "skip_required": "skip" in required_evaluators(assessment),
        "required_evaluators": list(required_evaluators(assessment)),
        "evaluation_complete": evaluation_is_complete(assessment),
        "reporting_manager": serialize_employee(assessment.reporting_manager),
        "skip_level_manager": serialize_employee(assessment.skip_level_manager),
        "preference": build_preference_payload(assessment, preference, states),
        "questions": [serialize_question_state(state) for state in states]
    }

def validate_revision(preference, revision):
    if preference.updated_at != revision:
        raise PotentialDescriptiveConflict({
            "message": "Descriptive scoring data has changed. Refresh before saving.",
            "current_revision": preference.updated_at.isoformat()
        })

def validate_scoring_available(assessment, preference):
    if not assessment.is_published:
        raise ValidationError("The Potential questionnaire has not been published.")
    if not preference.is_enabled:
        raise ValidationError("Enable Potential descriptive scoring from the Scoring Console first.")
    if not evaluation_is_complete(assessment):
        raise ValidationError("All required managers must submit their evaluations before descriptive scoring.")

def require_invalidation_confirmation(assessment, confirmed):
    dependencies = potential_dependency_state(assessment.employee_questionnaire_id)
    if any(dependencies.values()) and not confirmed:
        raise PotentialInvalidationConfirmationRequired({
            "code": "invalidation_confirmation_required",
            "message": "This change will invalidate existing Potential-dependent results.",
            "dependent_results": dependencies
        })

def create_history(preference, action, admin, previous_status, previous_score, details):
    EmployeePotentialDescriptiveAssessmentHistory.objects.create(
        assessment=preference, action=action,
        previous_status=previous_status, new_status=preference.status,
        previous_score=previous_score, new_score=preference.descriptive_score,
        change_details=details, changed_by=admin
    )

def reset_calculated_descriptive_state(preference, admin):
    preference.status = "draft"
    preference.descriptive_score = None
    preference.objective_weight_used = None
    preference.descriptive_weight_used = None
    preference.assessed_by = admin
    preference.assessed_at = timezone.now()
    preference.save(update_fields=[
        "status", "descriptive_score", "objective_weight_used",
        "descriptive_weight_used", "assessed_by", "assessed_at", "updated_at"
    ])

@transaction.atomic
def retrieve_preference(assessment_id):
    assessment = get_assessment(assessment_id, lock=True)
    preference = get_preference(assessment)
    return build_preference_payload(assessment, preference)

@transaction.atomic
def retrieve_workspace(assessment_id):
    assessment = get_assessment(assessment_id, lock=True)
    preference = get_preference(assessment)
    return build_workspace_payload(assessment, preference)

@transaction.atomic
def set_preference(assessment_id, enabled, admin, confirm_invalidation=False):
    assessment = get_assessment(assessment_id, lock=True)
    preference = get_preference(assessment)
    previous_enabled = preference.is_enabled
    if previous_enabled == enabled:
        return {
            "changed": False, "invalidation": empty_invalidation_result(),
            "preference": build_preference_payload(assessment, preference)
        }
    if enabled and not get_descriptive_items(assessment):
        raise ValidationError("This Potential questionnaire does not contain descriptive questions.")
    require_invalidation_confirmation(assessment, confirm_invalidation)
    previous_status, previous_score = preference.status, preference.descriptive_score
    preference.is_enabled = enabled
    preference.status = "draft"
    preference.descriptive_score = None
    preference.objective_weight_used = None
    preference.descriptive_weight_used = None
    preference.assessed_by = admin
    preference.assessed_at = timezone.now()
    preference.save(update_fields=[
        "is_enabled", "status", "descriptive_score", "objective_weight_used",
        "descriptive_weight_used", "assessed_by", "assessed_at", "updated_at"
    ])
    invalidation = invalidate_potential_dependencies(assessment, admin=admin)
    create_history(preference, "preference_changed", admin, previous_status, previous_score, {
        "previous_enabled": previous_enabled, "new_enabled": enabled,
        "invalidated_results": invalidation["affected_results"]
    })
    return {
        "changed": True, "invalidation": invalidation,
        "preference": build_preference_payload(assessment, preference)
    }

def validate_score_rows(states, scores):
    state_map = {state["questionnaire_item_id"]: state for state in states}
    changed_rows, seen = [], set()
    for row in scores:
        item_id = row["questionnaire_item_id"]
        if item_id in seen:
            raise ValidationError("Duplicate descriptive question scores are not allowed.")
        seen.add(item_id)
        state = state_map.get(item_id)
        if state is None:
            raise ValidationError({
                "questionnaire_item_id": item_id,
                "message": "This is not a descriptive question in the selected assessment."
            })
        if not state["can_score"]:
            raise ValidationError({
                "questionnaire_item_id": item_id,
                "message": "All required manager responses must be submitted before scoring this question."
            })
        if isinstance(row["score"], bool) or not isinstance(row["score"], int) or not 1 <= row["score"] <= state["max_marks"]:
            raise ValidationError({
                "questionnaire_item_id": item_id,
                "message": f"Score must be an integer between 1 and {state['max_marks']}."
            })
        remarks = row.get("remarks") or ""
        if not isinstance(remarks, str):
            raise ValidationError("Remarks must be text.")
        normalized_row = {**row, "remarks": remarks}
        if not state["is_scored"] or state["score"] != row["score"] or state["remarks"] != remarks:
            changed_rows.append((state, normalized_row))
    return changed_rows

def numerical_score_changed(state, row):
    marks = [get_response_mark(response) for response in state["responses"]]
    return any(mark is None or mark.score != row["score"] for mark in marks)

def persist_descriptive_mark(response, row, admin):
    try:
        EmployeePotentialDescriptiveScore.objects.update_or_create(
            response_id=response.pk,
            defaults={
                "score": row["score"], "remarks": row["remarks"], "scored_by": admin
            }
        )
    except DjangoValidationError as exc:
        raise ValidationError(exc.message_dict if hasattr(exc, "message_dict") else exc.messages)

@transaction.atomic
def save_descriptive_scores(assessment_id, scores, revision, admin, confirm_invalidation=False):
    assessment = get_assessment(assessment_id, lock=True)
    preference = get_preference(assessment)
    validate_scoring_available(assessment, preference)
    validate_revision(preference, revision)
    states = get_question_states(assessment)
    changed_rows = validate_score_rows(states, scores)
    if not changed_rows:
        return {
            "changed": False, "updated_count": 0,
            "invalidation": empty_invalidation_result(),
            "workspace": build_workspace_payload(assessment, preference)
        }
    affects_calculation = any(
        numerical_score_changed(state, row) or state["has_conflict"]
        for state, row in changed_rows
    )
    if affects_calculation:
        require_invalidation_confirmation(assessment, confirm_invalidation)
    previous_status, previous_score = preference.status, preference.descriptive_score
    history_rows = []
    for state, row in changed_rows:
        for response in state["responses"]:
            persist_descriptive_mark(response, row, admin)
        history_rows.append({
            "questionnaire_item_id": state["questionnaire_item_id"],
            "previous_score": state["score"], "new_score": row["score"],
            "previous_remarks": state["remarks"], "new_remarks": row["remarks"]
        })
    if affects_calculation:
        reset_calculated_descriptive_state(preference, admin)
        invalidation = invalidate_potential_dependencies(assessment, admin=admin)
    else:
        preference.save(update_fields=["updated_at"])
        invalidation = empty_invalidation_result()
    create_history(preference, "scores_updated", admin, previous_status, previous_score, {
        "questions": history_rows,
        "affects_calculation": affects_calculation,
        "invalidated_results": invalidation["affected_results"]
    })
    return {
        "changed": True, "updated_count": len(changed_rows),
        "invalidation": invalidation,
        "workspace": build_workspace_payload(assessment, preference)
    }

@transaction.atomic
def clear_descriptive_scores(assessment_id, item_ids, revision, admin, confirm_invalidation=False):
    assessment = get_assessment(assessment_id, lock=True)
    preference = get_preference(assessment)
    validate_scoring_available(assessment, preference)
    validate_revision(preference, revision)
    states = get_question_states(assessment)
    state_map = {state["questionnaire_item_id"]: state for state in states}
    missing_ids = set(item_ids) - set(state_map)
    if missing_ids:
        raise ValidationError({"questionnaire_item_ids": f"Invalid descriptive questions: {sorted(missing_ids)}"})
    marks = EmployeePotentialDescriptiveScore.objects.filter(
        response__assessment_id=assessment.pk,
        response__questionnaire_item_id__in=item_ids
    )
    affected_ids = list(marks.values_list("response__questionnaire_item_id", flat=True).distinct())
    if not affected_ids:
        return {
            "changed": False, "cleared_count": 0,
            "invalidation": empty_invalidation_result(),
            "workspace": build_workspace_payload(assessment, preference)
        }
    require_invalidation_confirmation(assessment, confirm_invalidation)
    previous_status, previous_score = preference.status, preference.descriptive_score
    marks.delete()
    reset_calculated_descriptive_state(preference, admin)
    invalidation = invalidate_potential_dependencies(assessment, admin=admin)
    create_history(preference, "scores_cleared", admin, previous_status, previous_score, {
        "questionnaire_item_ids": affected_ids,
        "invalidated_results": invalidation["affected_results"]
    })
    return {
        "changed": True, "cleared_count": len(affected_ids),
        "invalidation": invalidation,
        "workspace": build_workspace_payload(assessment, preference)
    }

@transaction.atomic
def get_descriptive_history(assessment_id):
    assessment = get_assessment(assessment_id, lock=True)
    preference = get_preference(assessment)
    history = preference.history.select_related("changed_by").order_by("-changed_at", "-id")
    return [{
        "id": row.pk, "action": row.action,
        "previous_status": row.previous_status, "new_status": row.new_status,
        "previous_score": row.previous_score, "new_score": row.new_score,
        "change_details": row.change_details,
        "changed_by": serialize_employee(row.changed_by),
        "changed_at": row.changed_at
    } for row in history]