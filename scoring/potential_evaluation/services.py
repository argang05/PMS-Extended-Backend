import logging
from math import isfinite
from django.db import transaction
from django.db.models import Q, Count, Exists, OuterRef
from django.utils import timezone
from django.core.exceptions import ValidationError as DjangoValidationError
from rest_framework.exceptions import ValidationError, PermissionDenied, NotFound, APIException
from rest_framework.fields import DateTimeField
from evaluations.models import EmployeeQuestionnaire
from scoring.models import PotentialParameter, EmployeePotentialAssessment, EmployeePotentialQuestionnaireItem, EmployeePotentialResponse
from scoring.scoring_locks import lock_potential_assessment
from scoring.potential_questionnaire.services import ensure_default_assessment, QuestionnaireConflict, PotentialResetRequired
from scoring.potential_question_bank.services import resolve_automatic_questions
from .serializers import PotentialEvaluationAnswerSerializer

logger = logging.getLogger(__name__)

class PotentialEvaluationConflict(APIException):
    status_code = 409
    default_detail = "The assessment has changed. Refresh before submitting."
    default_code = "potential_evaluation_conflict"

def initialize_manager_assessments(user):
    if not user.is_authenticated or not user.is_active:
        raise PermissionDenied("An active employee account is required.")
    existing_assessments = EmployeePotentialAssessment.objects.filter(
        employee_questionnaire_id=OuterRef("pk")
    )
    published_assessments = existing_assessments.filter(is_published=True)
    candidates = EmployeeQuestionnaire.objects.filter(
        Q(employee__reporting_manager_id=user.pk) | Q(employee__skip_level_manager_id=user.pk),
        employee__is_active=True,
        is_published=True
    ).exclude(
        employee_id=user.pk
    ).exclude(
        status="draft"
    ).annotate(
        has_published_potential=Exists(published_assessments)
    ).filter(
        has_published_potential=False
    ).order_by("pk").values_list("pk", flat=True)
    initialized = []
    for questionnaire_id in list(candidates):
        try:
            assessment = ensure_default_assessment(questionnaire_id)
            if assessment and assessment.is_published:
                initialized.append(assessment.pk)
        except (ValidationError, QuestionnaireConflict, PotentialResetRequired, NotFound) as exc:
            logger.warning(
                "Potential assessment initialization skipped for questionnaire %s: %s",
                questionnaire_id, str(exc)
            )
    return initialized

def manager_assessment_queryset(user):
    initialize_manager_assessments(user)
    return EmployeePotentialAssessment.objects.filter(
        Q(reporting_manager_id=user.pk) | Q(skip_level_manager_id=user.pk),
        is_published=True
    ).exclude(employee_id=user.pk).select_related(
        "employee", "review_cycle", "reporting_manager", "skip_level_manager", "employee_questionnaire"
    ).annotate(question_count=Count("items", distinct=True)).order_by("-id")

def get_assessment(assessment_id, lock=False):
    if lock:
        _, assessment = lock_potential_assessment(assessment_id)
        return assessment
    try:
        return EmployeePotentialAssessment.objects.get(pk=assessment_id)
    except EmployeePotentialAssessment.DoesNotExist:
        raise NotFound("Potential assessment not found.")

def resolve_evaluator(assessment, user):
    if not user.is_authenticated or not user.is_active:
        raise PermissionDenied("An active employee account is required.")
    if assessment.employee_id == user.pk:
        raise PermissionDenied("Employees cannot evaluate their own potential.")
    if not assessment.is_published:
        raise PermissionDenied("This Potential questionnaire is not currently available for evaluation.")
    is_rm = assessment.reporting_manager_id == user.pk
    is_skip = assessment.skip_level_manager_id == user.pk
    if is_rm and is_skip:
        raise ValidationError("The assessment contains conflicting manager assignments.")
    if is_rm:
        return "rm"
    if is_skip:
        return "skip"
    raise PermissionDenied("You are not an assigned evaluator for this assessment.")

def required_evaluators(assessment):
    return ("rm", "skip") if assessment.skip_level_manager_id or assessment.skip_submitted else ("rm",)

def evaluation_is_complete(assessment):
    return bool(
        assessment.is_published and assessment.rm_submitted
        and ("skip" not in required_evaluators(assessment) or assessment.skip_submitted)
        and assessment.status == "completed"
    )

def evaluator_has_submitted(assessment, evaluator):
    if evaluator not in ("rm", "skip"):
        return False
    return assessment.rm_submitted if evaluator == "rm" else assessment.skip_submitted

def evaluator_can_submit(assessment, evaluator):
    if evaluator not in required_evaluators(assessment):
        return False
    if not assessment.is_published or evaluator_has_submitted(assessment, evaluator):
        return False
    if evaluator == "rm":
        return assessment.status == "pending_rm"
    return bool(assessment.skip_level_manager_id and assessment.status == "pending_skip" and assessment.rm_submitted)

def validate_submission_stage(assessment, evaluator):
    if evaluator_has_submitted(assessment, evaluator):
        raise PotentialEvaluationConflict("Your Potential evaluation has already been submitted.")
    if not evaluator_can_submit(assessment, evaluator):
        if evaluator == "skip" and not assessment.rm_submitted:
            raise ValidationError("The Reporting Manager must complete the evaluation first.")
        raise ValidationError("This assessment is not currently accepting your evaluation.")

def get_assessment_items(assessment):
    return list(assessment.items.select_related("parameter", "parameter__category").order_by("order", "id"))

def is_legacy_assessment(assessment):
    return assessment.is_published and assessment.published_at is None

def materialize_legacy_items(assessment):
    existing = get_assessment_items(assessment)
    if existing or not is_legacy_assessment(assessment):
        return existing
    if assessment.rm_submitted or assessment.skip_submitted:
        return existing
    if EmployeePotentialResponse.objects.filter(assessment_id=assessment.pk).exists():
        raise PotentialEvaluationConflict("Historical responses require administrative reconciliation before evaluation.")
    automatic_ids = list(
        resolve_automatic_questions(assessment.employee).values_list("pk", flat=True)
    )
    parameters = list(PotentialParameter.objects.select_for_update().filter(
        pk__in=automatic_ids, is_active=True
    ).order_by("pk"))
    if not parameters:
        return existing
    if not any(parameter.question_type == "objective" for parameter in parameters):
        raise ValidationError("At least one applicable objective Potential question is required.")
    rows = []
    for order, parameter in enumerate(parameters, start=1):
        if not isfinite(parameter.weightage) or parameter.weightage <= 0:
            raise ValidationError(f"Question {parameter.pk} has an invalid weightage.")
        if parameter.max_marks < 1:
            raise ValidationError(f"Question {parameter.pk} has invalid maximum marks.")
        if parameter.question_type not in ("objective", "descriptive"):
            raise ValidationError(f"Question {parameter.pk} has an unsupported question type.")
        rows.append(EmployeePotentialQuestionnaireItem(
            assessment_id=assessment.pk, parameter_id=parameter.pk,
            weightage=parameter.weightage, order=order, source="automatic",
            question_text_snapshot=parameter.question_text,
            question_type_snapshot=parameter.question_type,
            max_marks_snapshot=parameter.max_marks,
            category_name_snapshot=parameter.category.name if parameter.category_id else None
        ))
    if rows:
        EmployeePotentialQuestionnaireItem.objects.bulk_create(rows, batch_size=1000)
        assessment.save(update_fields=["updated_at"])
    return get_assessment_items(assessment)

def serialize_question(item):
    return {
        "id": item.pk, "questionnaire_item_id": item.pk,
        "parameter_id": item.parameter_id,
        "question_text": item.effective_question_text,
        "question_type": item.effective_question_type,
        "max_marks": item.effective_max_marks,
        "category_name": item.category_name_snapshot,
        "weightage": item.weightage, "order": item.order
    }

def get_evaluator_responses(assessment, evaluator):
    return list(EmployeePotentialResponse.objects.filter(
        assessment_id=assessment.pk, evaluator_type=evaluator
    ).order_by("questionnaire_item_id", "id"))

def serialize_response(response):
    return {
        "id": response.pk,
        "questionnaire_item_id": response.questionnaire_item_id,
        "parameter_id": response.parameter_id,
        "score": response.score,
        "descriptive_answer": response.descriptive_answer,
        "is_not_applicable": response.is_not_applicable,
        "evaluator_type": response.evaluator_type,
        "submitted_at": response.submitted_at
    }

def build_evaluation_detail(assessment, evaluator, items):
    responses = get_evaluator_responses(assessment, evaluator)
    employee = assessment.employee
    return {
        "id": assessment.pk,
        "assessment_id": assessment.pk,
        "employee_questionnaire_id": assessment.employee_questionnaire_id,
        "employee": {
            "id": employee.pk, "employee_number": employee.employee_number,
            "first_name": employee.first_name, "last_name": employee.last_name,
            "full_name": f"{employee.first_name or ''} {employee.last_name or ''}".strip(),
            "department": employee.department, "designation": employee.designation
        },
        "review_cycle_id": assessment.review_cycle_id,
        "review_cycle_name": assessment.review_cycle.name,
        "status": assessment.status,
        "is_published": assessment.is_published,
        "evaluator_type": evaluator,
        "can_evaluate": evaluator_can_submit(assessment, evaluator) and bool(items),
        "is_submitted": evaluator_has_submitted(assessment, evaluator),
        "rm_submitted": assessment.rm_submitted,
        "skip_submitted": assessment.skip_submitted,
        "skip_required": "skip" in required_evaluators(assessment),
        "required_evaluators": list(required_evaluators(assessment)),
        "evaluation_complete": evaluation_is_complete(assessment),
        "rm_submitted_at": assessment.rm_submitted_at,
        "skip_submitted_at": assessment.skip_submitted_at,
        "question_count": len(items),
        "objective_count": sum(item.effective_question_type == "objective" for item in items),
        "descriptive_count": sum(item.effective_question_type == "descriptive" for item in items),
        "requires_preparation": not items and is_legacy_assessment(assessment)
            and evaluator_can_submit(assessment, evaluator),
        "revision": assessment.updated_at.isoformat(),
        "questions": [serialize_question(item) for item in items],
        "responses": [serialize_response(response) for response in responses]
    }

@transaction.atomic
def get_evaluation_detail(assessment_id, user):
    assessment = get_assessment(assessment_id, lock=True)
    evaluator = resolve_evaluator(assessment, user)
    if evaluator == "skip" and not assessment.rm_submitted:
        raise ValidationError("The Reporting Manager must complete the evaluation first.")
    return build_evaluation_detail(assessment, evaluator, get_assessment_items(assessment))

@transaction.atomic
def prepare_legacy_evaluation(assessment_id, user):
    assessment = get_assessment(assessment_id, lock=True)
    evaluator = resolve_evaluator(assessment, user)
    validate_submission_stage(assessment, evaluator)
    items = materialize_legacy_items(assessment)
    if not items:
        raise ValidationError("No questions are available for this Potential assessment.")
    return build_evaluation_detail(assessment, evaluator, items)

def validate_answer(item, response):
    question_type = item.effective_question_type
    max_marks = item.effective_max_marks
    score = response.get("score")
    raw_answer = response.get("descriptive_answer")
    if raw_answer is not None and not isinstance(raw_answer, str):
        raise ValidationError("Descriptive answers must be strings.")
    answer = (raw_answer or "").strip()
    if question_type == "objective":
        if score is None or isinstance(score, bool):
            raise ValidationError({"questionnaire_item_id": item.pk, "message": "A numerical score is required."})
        try:
            score = float(score)
        except (TypeError, ValueError, OverflowError):
            raise ValidationError({"questionnaire_item_id": item.pk, "message": "Invalid numerical score."})
        if not isfinite(score) or not 0 <= score <= max_marks:
            raise ValidationError({
                "questionnaire_item_id": item.pk,
                "message": f"Score must be between 0 and {max_marks}."
            })
        if answer:
            raise ValidationError({"questionnaire_item_id": item.pk, "message": "Objective questions cannot contain descriptive answers."})
        return score, None
    if question_type == "descriptive":
        if score is not None:
            raise ValidationError({"questionnaire_item_id": item.pk, "message": "Descriptive questions cannot contain numerical scores."})
        if not answer:
            raise ValidationError({"questionnaire_item_id": item.pk, "message": "A descriptive answer is required."})
        return None, answer
    raise ValidationError({"questionnaire_item_id": item.pk, "message": "Unsupported question type."})

def positive_id(value, field):
    if isinstance(value, bool) or not str(value or "").isdigit() or int(value) <= 0:
        raise ValidationError({field: "A valid positive ID is required."})
    return int(value)

def validate_revision(assessment, revision):
    if revision is None:
        raise PotentialEvaluationConflict({
            "message": "An evaluation revision is required. Refresh the questionnaire before continuing.",
            "current_revision": assessment.updated_at.isoformat()
        })
    if isinstance(revision, str):
        revision = DateTimeField().run_validation(revision)
    if assessment.updated_at != revision:
        raise PotentialEvaluationConflict({
            "message": "The assessment has changed. Refresh before continuing.",
            "current_revision": assessment.updated_at.isoformat()
        })

def normalize_responses(raw_responses, items):
    if not isinstance(raw_responses, list):
        raise ValidationError({"responses": "Responses must be an array."})
    if len(raw_responses) > 1000:
        raise ValidationError({"responses": "A maximum of 1000 responses is allowed."})
    item_map = {item.pk: item for item in items}
    parameter_map = {item.parameter_id: item for item in items}
    normalized, seen = [], set()
    for raw in raw_responses:
        if not isinstance(raw, dict):
            raise ValidationError("Every response must be an object.")
        item_id, parameter_id = raw.get("questionnaire_item_id"), raw.get("parameter_id")
        if item_id is not None:
            item = item_map.get(positive_id(item_id, "questionnaire_item_id"))
        elif parameter_id is not None:
            item = parameter_map.get(positive_id(parameter_id, "parameter_id"))
        else:
            raise ValidationError("A questionnaire item ID or parameter ID is required.")
        if item is None:
            raise ValidationError("Response refers to a question not assigned to this assessment.")
        if parameter_id is not None and positive_id(parameter_id, "parameter_id") != item.parameter_id:
            raise ValidationError("Questionnaire item and parameter IDs do not match.")
        if item.pk in seen:
            raise ValidationError("Duplicate question responses are not allowed.")
        if raw.get("is_not_applicable"):
            raise ValidationError("N/A is not supported in Potential evaluation.")
        if isinstance(raw.get("score"), bool):
            raise ValidationError("Boolean values are not valid scores.")
        seen.add(item.pk)
        serializer = PotentialEvaluationAnswerSerializer(data={
            "questionnaire_item_id": item.pk,
            "score": raw.get("score"),
            "descriptive_answer": raw.get("descriptive_answer")
        })
        serializer.is_valid(raise_exception=True)
        score, answer = validate_answer(item, serializer.validated_data)
        normalized.append((item, score, answer))
    return normalized

def validate_existing_responses(assessment, evaluator, items, user):
    item_map = {item.pk: item for item in items}
    responses = EmployeePotentialResponse.objects.select_for_update().filter(
        assessment_id=assessment.pk, evaluator_type=evaluator
    ).order_by("pk")
    existing = {}
    for response in responses:
        item_id = response.questionnaire_item_id
        if item_id is None or item_id not in item_map:
            raise PotentialEvaluationConflict("Historical responses require administrative reconciliation.")
        if response.parameter_id != item_map[item_id].parameter_id or item_id in existing:
            raise PotentialEvaluationConflict("Existing Potential responses contain conflicting question assignments.")
        if response.submitted_by_id not in (None, user.pk):
            raise PermissionDenied("An existing response belongs to another evaluator.")
        existing[item_id] = response
    return existing

def persist_response(response):
    try:
        response.full_clean()
        response.save()
    except DjangoValidationError as exc:
        raise ValidationError(exc.message_dict if hasattr(exc, "message_dict") else exc.messages)

@transaction.atomic
def save_evaluation(assessment_id, user, responses, revision=None, submit=False, expected_evaluator=None, allow_legacy_preparation=False):
    assessment = get_assessment(assessment_id, lock=True)
    evaluator = resolve_evaluator(assessment, user)
    if expected_evaluator is not None and evaluator != expected_evaluator:
        raise PermissionDenied("You are not authorized for this evaluation stage.")
    validate_revision(assessment, revision)
    validate_submission_stage(assessment, evaluator)
    items = get_assessment_items(assessment)
    if not items and allow_legacy_preparation:
        items = materialize_legacy_items(assessment)
    if not items:
        raise ValidationError("No questions are assigned to this Potential assessment.")
    normalized = normalize_responses(responses, items)
    existing = validate_existing_responses(assessment, evaluator, items, user)
    changed = False
    for item, score, answer in normalized:
        response = existing.get(item.pk)
        if response is not None:
            if response.score == score and response.descriptive_answer == answer and response.submitted_by_id == user.pk and not response.is_not_applicable:
                continue
            response.score = score
            response.descriptive_answer = answer
            response.submitted_by = user
            response.is_not_applicable = False
        else:
            response = EmployeePotentialResponse(
                assessment_id=assessment.pk, parameter_id=item.parameter_id,
                questionnaire_item_id=item.pk, evaluator_type=evaluator,
                submitted_by=user, score=score, descriptive_answer=answer,
                is_not_applicable=False
            )
        persist_response(response)
        existing[item.pk] = response
        changed = True
    if submit:
        missing_ids = [item.pk for item in items if item.pk not in existing]
        if missing_ids:
            raise ValidationError({
                "message": "Complete every assigned question before submission.",
                "missing_questionnaire_item_ids": missing_ids
            })
        for item in items:
            response = existing[item.pk]
            if response.is_not_applicable:
                raise ValidationError("N/A is not supported in Potential evaluation.")
            validate_answer(item, {"score": response.score, "descriptive_answer": response.descriptive_answer})
        now = timezone.now()
        EmployeePotentialResponse.objects.filter(
            assessment_id=assessment.pk, evaluator_type=evaluator
        ).update(submitted_at=now, submitted_by=user)
        if evaluator == "rm":
            assessment.rm_submitted = True
            assessment.rm_submitted_at = now
            assessment.status = "pending_skip" if assessment.skip_level_manager_id else "completed"
            fields = ["rm_submitted", "rm_submitted_at", "status", "updated_at"]
        else:
            assessment.skip_submitted = True
            assessment.skip_submitted_at = now
            assessment.status = "completed"
            fields = ["skip_submitted", "skip_submitted_at", "status", "updated_at"]
        assessment.save(update_fields=fields)
        changed = True
    elif changed:
        assessment.save(update_fields=["updated_at"])
    return {**build_evaluation_detail(assessment, evaluator, items), "changed": changed}

def save_evaluation_draft(assessment_id, user, revision, responses):
    return save_evaluation(assessment_id, user, responses, revision=revision)

def submit_evaluation(assessment_id, user, revision, responses):
    return save_evaluation(assessment_id, user, responses, revision=revision, submit=True)