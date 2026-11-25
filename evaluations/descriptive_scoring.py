from django.db import transaction
from django.core.exceptions import ValidationError
from django.utils import timezone
from .models import EmployeeQuestionnaireResponse, EmployeeDescriptiveScore
from scoring.models import EmployeePerformanceScore, EmployeeAnalyticsRelease


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


def _invalidate(questionnaire_ids, admin):
    if not questionnaire_ids:
        return

    EmployeePerformanceScore.objects.filter(
        employee_questionnaire_id__in=questionnaire_ids
    ).update(
        is_released_to_employee=False,
        released_by=None,
        released_at=None
    )

    EmployeeAnalyticsRelease.objects.filter(
        employee_questionnaire_id__in=questionnaire_ids
    ).update(
        is_released_to_employee=False,
        unreleased_at=timezone.now(),
        unreleased_by=admin
    )


@transaction.atomic
def save_descriptive_score(response_id, score, admin, remarks=""):
    response = EmployeeQuestionnaireResponse.objects.select_related(
        "submission", "questionnaire_item__parameter"
    ).select_for_update().get(pk=response_id)

    _validate_score(score, _validate_response(response))

    if not isinstance(remarks, str):
        raise ValidationError("Remarks must be text.")

    old = EmployeeDescriptiveScore.objects.filter(response=response).first()
    changed = old is None or old.score != score

    record, _ = EmployeeDescriptiveScore.objects.update_or_create(
        response=response,
        defaults={
            "score": score,
            "scored_by": admin,
            "remarks": remarks
        }
    )

    if changed:
        _invalidate({response.submission.employee_questionnaire_id}, admin)

    return _score_data(record)


@transaction.atomic
def save_descriptive_scores(entries, admin):
    if not isinstance(entries, list) or not entries:
        raise ValidationError("Provide at least one descriptive score.")

    if any(not isinstance(entry, dict) for entry in entries):
        raise ValidationError("Each descriptive score entry must be an object.")

    ids = [entry.get("response_id") for entry in entries]

    if any(isinstance(value, bool) or not isinstance(value, int) for value in ids):
        raise ValidationError("response_id must be an integer.")

    if len(ids) != len(set(ids)):
        raise ValidationError("Duplicate response IDs are not allowed.")

    responses = {
        response.id: response
        for response in EmployeeQuestionnaireResponse.objects.select_related(
            "submission", "questionnaire_item__parameter"
        ).select_for_update().filter(id__in=ids)
    }

    if len(responses) != len(ids):
        raise ValidationError("One or more descriptive responses were not found.")

    validated = []

    for entry in entries:
        response = responses[entry["response_id"]]
        score = _validate_score(entry.get("score"), _validate_response(response))
        remarks = entry.get("remarks", "")

        if not isinstance(remarks, str):
            raise ValidationError("Remarks must be text.")

        validated.append((response, score, remarks))

    previous = {
        record.response_id: record.score
        for record in EmployeeDescriptiveScore.objects.filter(response_id__in=ids)
    }

    changed_questionnaires = set()
    results = []

    for response, score, remarks in validated:
        record, _ = EmployeeDescriptiveScore.objects.update_or_create(
            response=response,
            defaults={
                "score": score,
                "scored_by": admin,
                "remarks": remarks
            }
        )

        if previous.get(response.id) != score:
            changed_questionnaires.add(response.submission.employee_questionnaire_id)

        results.append(_score_data(record))

    _invalidate(changed_questionnaires, admin)
    return results