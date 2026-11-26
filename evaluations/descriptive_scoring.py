from django.db import transaction
from django.core.exceptions import ValidationError
from .models import EmployeeQuestionnaireResponse, EmployeeDescriptiveScore
from scoring.scoring_locks import lock_questionnaires
from scoring.descriptive_preference import (
    is_descriptive_scoring_enabled, invalidate_performance_results,
    reset_descriptive_calculation
)

def _validate_response(response):
    item = response.questionnaire_item
    question_type = item.question_type_snapshot or item.parameter.question_type
    if question_type != "descriptive":
        raise ValidationError("Only descriptive responses can receive admin scores.")
    if response.is_not_applicable:
        raise ValidationError("N/A responses cannot receive admin scores.")
    if not (response.descriptive_answer or "").strip():
        raise ValidationError("Cannot score an empty descriptive answer.")
    if response.submission.status != "submitted":
        raise ValidationError("Only submitted descriptive responses can be scored.")
    if item.employee_questionnaire_id != response.submission.employee_questionnaire_id:
        raise ValidationError("Response belongs to an inconsistent questionnaire item.")
    return item.max_marks_snapshot if item.max_marks_snapshot is not None else item.parameter.max_marks

def _validate_score(score, max_marks):
    if isinstance(score, bool) or not isinstance(score, int):
        raise ValidationError("Score must be an integer.")
    if not 0 <= score <= max_marks:
        raise ValidationError(f"Score must be between 0 and {max_marks}.")
    return score

def _score_data(record):
    return {
        "response_id": record.response_id,
        "score": record.score,
        "remarks": record.remarks,
        "scored_by_id": record.scored_by_id,
        "scored_at": record.scored_at,
        "updated_at": record.updated_at
    }

def _lock_questionnaires(questionnaire_ids):
    questionnaires = lock_questionnaires(questionnaire_ids)
    if set(questionnaires) != set(questionnaire_ids):
        raise ValidationError("One or more questionnaires were not found.")
    for questionnaire in questionnaires.values():
        if not is_descriptive_scoring_enabled(questionnaire):
            raise ValidationError(f"Descriptive scoring is disabled for questionnaire {questionnaire.pk}.")
    return questionnaires

def _validate_entries(entries):
    if not isinstance(entries, list) or not entries:
        raise ValidationError("Provide at least one descriptive score.")
    if any(not isinstance(entry, dict) for entry in entries):
        raise ValidationError("Each descriptive score entry must be an object.")
    ids = [entry.get("response_id") for entry in entries]
    if any(isinstance(value, bool) or not isinstance(value, int) or value <= 0 for value in ids):
        raise ValidationError("response_id must be a positive integer.")
    if len(ids) != len(set(ids)):
        raise ValidationError("Duplicate response IDs are not allowed.")
    return ids

def _save_record(response, score, remarks, admin, previous):
    if previous is not None and previous.score == score and previous.remarks == remarks:
        return previous, False
    numerical_change = previous is None or previous.score != score
    record, _ = EmployeeDescriptiveScore.objects.update_or_create(
        response=response, defaults={
            "score": score, "scored_by": admin, "remarks": remarks
        }
    )
    return record, numerical_change

@transaction.atomic
def save_descriptive_scores(entries, admin):
    ids = _validate_entries(entries)
    questionnaire_ids = set(EmployeeQuestionnaireResponse.objects.filter(
        id__in=ids
    ).values_list("submission__employee_questionnaire_id", flat=True))
    questionnaires = _lock_questionnaires(questionnaire_ids)
    responses = {
        response.pk: response
        for response in EmployeeQuestionnaireResponse.objects.select_related(
            "submission", "questionnaire_item__parameter"
        ).select_for_update(of=("self",)).filter(id__in=ids).order_by("pk")
    }
    if len(responses) != len(ids):
        raise ValidationError("One or more descriptive responses were not found.")
    validated = []
    for entry in entries:
        response = responses[entry["response_id"]]
        questionnaire_id = response.submission.employee_questionnaire_id
        if questionnaire_id not in questionnaires:
            raise ValidationError("Questionnaire assignment has changed. Refresh before saving.")
        score = _validate_score(entry.get("score"), _validate_response(response))
        remarks = entry.get("remarks", "")
        if not isinstance(remarks, str):
            raise ValidationError("Remarks must be text.")
        validated.append((response, score, remarks))
    previous = {
        record.response_id: record
        for record in EmployeeDescriptiveScore.objects.select_for_update().filter(
            response_id__in=ids
        ).order_by("pk")
    }
    changed_questionnaires, results = set(), []
    for response, score, remarks in validated:
        record, numerical_change = _save_record(
            response, score, remarks, admin, previous.get(response.pk)
        )
        if numerical_change:
            changed_questionnaires.add(response.submission.employee_questionnaire_id)
        results.append(_score_data(record))
    for questionnaire_id in sorted(changed_questionnaires):
        questionnaire = questionnaires[questionnaire_id]
        reset_descriptive_calculation(questionnaire, admin)
        invalidate_performance_results(questionnaire, admin)
    return results

@transaction.atomic
def save_descriptive_score(response_id, score, admin, remarks=""):
    results = save_descriptive_scores([{
        "response_id": response_id, "score": score, "remarks": remarks
    }], admin)
    return results[0]