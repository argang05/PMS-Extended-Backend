from django.db import transaction
from django.utils import timezone
from django.shortcuts import get_object_or_404
from evaluations.models import (
    QuestionParameter, EmployeeQuestionnaire, EmployeeQuestionnaireItem,
    EmployeeQuestionnaireCustomization, EmployeeQuestionnaireStageSubmission,
    EmployeeQuestionnaireResponse
)

class QuestionnaireBuilderError(Exception):
    pass

EDIT_LOCK_MESSAGE = (
    "Changes won't be reflected in the self questionnaire again for this quarter cycle. "
    "The employee has already submitted their self-review. To make changes, request "
    "an administrator to reset PMS and inform the concerned employee."
)

def _error(message):
    raise QuestionnaireBuilderError(message)

def _positive_int(value, name):
    if isinstance(value, bool):
        _error(f"{name} must be a positive integer.")
    try:
        number = int(value)
    except (TypeError, ValueError, OverflowError):
        _error(f"{name} must be a positive integer.")
    if str(value).strip() != str(number) or number <= 0:
        _error(f"{name} must be a positive integer.")
    return number

def _positive_weight(value):
    from math import isfinite
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        _error("Weightage must be a positive number.")
    if isinstance(value, bool) or not isfinite(number) or number <= 0:
        _error("Weightage must be a positive finite number.")
    return number

def _self_review_submitted(questionnaire):
    if questionnaire.status != "draft" or questionnaire.submitted_at:
        return True
    return EmployeeQuestionnaireStageSubmission.objects.filter(
        employee_questionnaire=questionnaire, evaluator_type="self", status="submitted"
    ).exists()

def _editable(questionnaire):
    if _self_review_submitted(questionnaire):
        _error(EDIT_LOCK_MESSAGE)
    if EmployeeQuestionnaireStageSubmission.objects.filter(
        employee_questionnaire=questionnaire, status="submitted"
    ).exists():
        _error(EDIT_LOCK_MESSAGE)

def _lock(questionnaire_id):
    questionnaire = get_object_or_404(
        EmployeeQuestionnaire.objects.select_for_update(of=("self",)).select_related("employee", "review_cycle"),
        id=questionnaire_id
    )
    _editable(questionnaire)
    return questionnaire

def _snapshot(question):
    if question.category_id is None:
        _error(f"Question #{question.pk} must have a category before assignment.")
    return {
        "question_text_snapshot": question.question_text,
        "question_type_snapshot": question.question_type,
        "max_marks_snapshot": question.max_marks,
        "category_type_snapshot": question.category.category_type
    }

def _create_item(questionnaire, question, order, source="automatic", weightage=None):
    defaults = {
        "weightage": question.default_weightage if weightage is None else _positive_weight(weightage),
        "order": order, "source": source, **_snapshot(question)
    }
    return EmployeeQuestionnaireItem.objects.get_or_create(
        employee_questionnaire=questionnaire, parameter=question, defaults=defaults
    )

def _renumber(questionnaire):
    items = list(EmployeeQuestionnaireItem.objects.filter(
        employee_questionnaire=questionnaire
    ).order_by("order", "id"))
    changed = []
    for index, item in enumerate(items, 1):
        if item.order != index:
            item.order = index
            changed.append(item)
    if changed:
        EmployeeQuestionnaireItem.objects.bulk_update(changed, ["order"])

def _department_matches(question, department):
    return any(
        assignment.department.strip().casefold() == (department or "").strip().casefold()
        for assignment in question.department_assignments.all()
    )

def _question_targets_employee(question, employee):
    if question.category_id is None:
        return False
    mode = question.target_mode
    department = (employee.department or "").strip()
    if mode == "legacy":
        if question.category.category_type == "behavioral":
            return question.department is None
        if question.category.category_type == "performance":
            return bool(question.department and question.department.strip().casefold() == department.casefold())
        return False
    excluded = any(a.employee_id == employee.pk for a in question.employee_exclusions.all())
    if excluded:
        return False
    if mode == "all":
        return True
    department_match = _department_matches(question, department)
    employee_match = any(a.employee_id == employee.pk for a in question.employee_assignments.all())
    if mode == "departments":
        return department_match
    if mode == "employees":
        return employee_match
    if mode == "combined":
        return department_match or employee_match
    return False

def applicable_questions(employee):
    questions = list(
        QuestionParameter.objects.filter(is_active=True, category__isnull=False)
        .select_related("category")
        .prefetch_related("department_assignments", "employee_assignments", "employee_exclusions")
        .order_by("id")
    )
    legacy_behavioral, legacy_performance, targeted = [], [], []
    for question in questions:
        if not _question_targets_employee(question, employee):
            continue
        if question.target_mode != "legacy":
            targeted.append(question)
        elif question.category.category_type == "behavioral":
            legacy_behavioral.append(question)
        elif question.category.category_type == "performance":
            legacy_performance.append(question)
    return legacy_behavioral[:5] + legacy_performance[:5] + targeted

def _initialize_untouched_questionnaire_locked(questionnaire):
    if questionnaire.status != "draft" or questionnaire.submitted_at:
        return 0
    if questionnaire.items.exists():
        return 0
    if EmployeeQuestionnaireCustomization.objects.filter(employee_questionnaire=questionnaire).exists():
        return 0
    if EmployeeQuestionnaireStageSubmission.objects.filter(employee_questionnaire=questionnaire).exists():
        return 0
    questions = applicable_questions(questionnaire.employee)
    if not questions:
        return 0
    items = [
        EmployeeQuestionnaireItem(
            employee_questionnaire=questionnaire,
            parameter=question,
            weightage=question.default_weightage,
            order=index,
            source="automatic",
            **_snapshot(question)
        )
        for index, question in enumerate(questions, 1)
    ]
    EmployeeQuestionnaireItem.objects.bulk_create(items)
    return len(items)

@transaction.atomic
def ensure_questionnaire_initialized(questionnaire_id):
    questionnaire = get_object_or_404(
        EmployeeQuestionnaire.objects.select_for_update(of=("self",))
        .select_related("employee", "review_cycle"),
        id=questionnaire_id
    )
    _initialize_untouched_questionnaire_locked(questionnaire)
    return questionnaire

def _customizations(questionnaire):
    return {
        item.parameter_id: item.action
        for item in EmployeeQuestionnaireCustomization.objects.filter(employee_questionnaire=questionnaire)
    }

def _sync_locked(questionnaire, preserve_manual=True):
    _editable(questionnaire)
    automatic = applicable_questions(questionnaire.employee)
    customizations = _customizations(questionnaire)
    desired = {
        question.pk: (question, "automatic")
        for question in automatic if customizations.get(question.pk) != "exclude"
    }
    if preserve_manual:
        manually_included = QuestionParameter.objects.filter(
            id__in=[pk for pk, action in customizations.items() if action == "include"],
            is_active=True, category__isnull=False
        ).select_related("category")
        for question in manually_included:
            desired[question.pk] = (question, "manual")
    existing = {
        item.parameter_id: item
        for item in EmployeeQuestionnaireItem.objects.filter(employee_questionnaire=questionnaire)
    }
    obsolete = [item.pk for pk, item in existing.items() if pk not in desired]
    if obsolete:
        EmployeeQuestionnaireItem.objects.filter(pk__in=obsolete).delete()
    next_order = max((item.order for item in existing.values()), default=0) + 1
    created = 0
    for question, source in desired.values():
        if question.pk in existing:
            item = existing[question.pk]
            if item.source != source:
                item.source = source
                item.save(update_fields=["source"])
            continue
        _, was_created = _create_item(questionnaire, question, next_order, source)
        if was_created:
            next_order += 1
            created += 1
    _renumber(questionnaire)
    return {
        "created": created, "removed": len(obsolete),
        "total": EmployeeQuestionnaireItem.objects.filter(employee_questionnaire=questionnaire).count()
    }

@transaction.atomic
def create_or_get_questionnaire(employee, review_cycle):
    questionnaire, created = EmployeeQuestionnaire.objects.get_or_create(
        employee=employee, review_cycle=review_cycle, defaults={"status": "draft"}
    )
    questionnaire = ensure_questionnaire_initialized(questionnaire.pk)
    return questionnaire, created

@transaction.atomic
def generate_questionnaire(questionnaire_id):
    questionnaire = _lock(questionnaire_id)
    return _sync_locked(questionnaire)

@transaction.atomic
def include_question(questionnaire_id, question_id, admin, weightage=None):
    questionnaire = _lock(questionnaire_id)
    question = get_object_or_404(
        QuestionParameter.objects.select_related("category"),
        id=question_id, is_active=True
    )
    if question.category_id is None:
        _error(f"Question #{question.pk} must be categorised before manual inclusion.")
    existing = EmployeeQuestionnaireItem.objects.filter(
        employee_questionnaire=questionnaire, parameter=question
    ).first()
    if existing:
        _error(f"Question #{question.pk} is already assigned to this employee's questionnaire.")
    if weightage is not None:
        weightage = _positive_weight(weightage)
    EmployeeQuestionnaireCustomization.objects.update_or_create(
        employee_questionnaire=questionnaire, parameter=question,
        defaults={"action": "include", "created_by": admin}
    )
    next_order = (
        EmployeeQuestionnaireItem.objects.filter(employee_questionnaire=questionnaire)
        .order_by("-order").values_list("order", flat=True).first() or 0
    ) + 1
    item, _ = _create_item(questionnaire, question, next_order, "manual", weightage)
    return item

@transaction.atomic
def exclude_question(questionnaire_id, question_id, admin):
    questionnaire = _lock(questionnaire_id)
    question = get_object_or_404(QuestionParameter, id=question_id)
    EmployeeQuestionnaireCustomization.objects.update_or_create(
        employee_questionnaire=questionnaire, parameter=question,
        defaults={"action": "exclude", "created_by": admin}
    )
    removed, _ = EmployeeQuestionnaireItem.objects.filter(
        employee_questionnaire=questionnaire, parameter=question
    ).delete()
    _renumber(questionnaire)
    return {"excluded_question_id": question.pk, "removed": bool(removed)}

@transaction.atomic
def reorder_questions(questionnaire_id, item_ids):
    questionnaire = _lock(questionnaire_id)
    if not isinstance(item_ids, list):
        _error("item_ids must be a list.")
    ids = [_positive_int(value, "item_id") for value in item_ids]
    if len(ids) != len(set(ids)):
        _error("Duplicate questionnaire item IDs are not allowed.")
    items = list(EmployeeQuestionnaireItem.objects.filter(employee_questionnaire=questionnaire))
    existing = {item.pk: item for item in items}
    if set(ids) != set(existing):
        _error("item_ids must contain every questionnaire item exactly once.")
    changed = []
    for position, pk in enumerate(ids, 1):
        item = existing[pk]
        if item.order != position:
            item.order = position
            changed.append(item)
    if changed:
        EmployeeQuestionnaireItem.objects.bulk_update(changed, ["order"])
    return {"updated": len(changed), "total": len(items)}

@transaction.atomic
def update_item_weightage(questionnaire_id, item_id, weightage):
    questionnaire = _lock(questionnaire_id)
    item = get_object_or_404(
        EmployeeQuestionnaireItem, id=item_id, employee_questionnaire=questionnaire
    )
    item.weightage = _positive_weight(weightage)
    item.save(update_fields=["weightage"])
    return item

@transaction.atomic
def reset_questionnaire_customizations(questionnaire_id):
    questionnaire = _lock(questionnaire_id)
    EmployeeQuestionnaireCustomization.objects.filter(
        employee_questionnaire=questionnaire
    ).delete()
    EmployeeQuestionnaireItem.objects.filter(
        employee_questionnaire=questionnaire
    ).delete()
    return _sync_locked(questionnaire, preserve_manual=False)

@transaction.atomic
def copy_questionnaire(source_id, destination_id, admin):
    source_id = _positive_int(source_id, "source_id")
    destination_id = _positive_int(destination_id, "destination_id")
    if source_id == destination_id:
        _error("Source and destination questionnaires must be different.")
    ids = sorted([source_id, destination_id])
    locked = {
        q.pk: q for q in EmployeeQuestionnaire.objects.select_for_update()
        .select_related("employee", "review_cycle").filter(pk__in=ids).order_by("pk")
    }
    source, destination = locked.get(source_id), locked.get(destination_id)
    if not source or not destination:
        _error("Source or destination questionnaire was not found.")
    _editable(destination)
    if source.review_cycle_id != destination.review_cycle_id:
        _error("Questionnaires must belong to the same review cycle.")
    source_items = list(
        EmployeeQuestionnaireItem.objects.filter(employee_questionnaire=source)
        .select_related("parameter", "parameter__category").order_by("order", "id")
    )
    if not source_items:
        _error("Source questionnaire has no questions.")
    source_exclusions = list(
        EmployeeQuestionnaireCustomization.objects.filter(
            employee_questionnaire=source, action="exclude"
        ).values_list("parameter_id", flat=True)
    )
    EmployeeQuestionnaireCustomization.objects.filter(
        employee_questionnaire=destination
    ).delete()
    EmployeeQuestionnaireItem.objects.filter(
        employee_questionnaire=destination
    ).delete()
    copied = [
        EmployeeQuestionnaireItem(
            employee_questionnaire=destination,
            parameter=item.parameter,
            weightage=item.weightage,
            order=position,
            source="manual",
            question_text_snapshot=item.question_text_snapshot or item.parameter.question_text,
            question_type_snapshot=item.question_type_snapshot or item.parameter.question_type,
            max_marks_snapshot=item.max_marks_snapshot or item.parameter.max_marks,
            category_type_snapshot=item.category_type_snapshot or (
                item.parameter.category.category_type if item.parameter.category_id else None
            )
        )
        for position, item in enumerate(source_items, 1)
    ]
    EmployeeQuestionnaireItem.objects.bulk_create(copied)
    assigned_ids = {item.parameter_id for item in source_items}
    customizations = [
        EmployeeQuestionnaireCustomization(
            employee_questionnaire=destination, parameter=item.parameter,
            action="include", created_by=admin
        )
        for item in source_items
    ]
    customizations.extend(
        EmployeeQuestionnaireCustomization(
            employee_questionnaire=destination, parameter_id=parameter_id,
            action="exclude", created_by=admin
        )
        for parameter_id in source_exclusions if parameter_id not in assigned_ids
    )
    EmployeeQuestionnaireCustomization.objects.bulk_create(customizations)
    return {
        "copied": len(copied), "excluded": len(source_exclusions),
        "destination_questionnaire_id": destination.pk
    }

@transaction.atomic
def publish_questionnaire(questionnaire_id, admin):
    questionnaire = _lock(questionnaire_id)
    if not questionnaire.items.exists():
        _error("Cannot publish a questionnaire without questions.")
    items = list(
        questionnaire.items.select_related("parameter", "parameter__category")
        .order_by("order", "id")
    )
    for item in items:
        if item.weightage <= 0:
            _error(f"Questionnaire item {item.pk} has an invalid weightage.")
        if item.parameter.category_id is None:
            _error(f"Question #{item.parameter_id} must be categorised before publication.")
        changed = []
        snapshots = _snapshot(item.parameter)
        for field, value in snapshots.items():
            if getattr(item, field) is None:
                setattr(item, field, value)
                changed.append(field)
        if changed:
            item.save(update_fields=changed)
    questionnaire.is_published = True
    questionnaire.published_at = timezone.now()
    questionnaire.published_by = admin
    questionnaire.save(update_fields=["is_published", "published_at", "published_by"])
    return questionnaire

def questionnaire_details(questionnaire):
    items = list(
        questionnaire.items.select_related("parameter", "parameter__category")
        .order_by("order", "id")
    )
    excluded_question_ids = list(
        questionnaire.customizations.filter(action="exclude")
        .order_by("parameter_id").values_list("parameter_id", flat=True)
    )
    self_review_submitted = _self_review_submitted(questionnaire)
    other_submitted = EmployeeQuestionnaireStageSubmission.objects.filter(
        employee_questionnaire=questionnaire, status="submitted"
    ).exclude(evaluator_type="self").exists()
    is_editable = not self_review_submitted and not other_submitted
    has_draft_responses = EmployeeQuestionnaireResponse.objects.filter(
        questionnaire_item__employee_questionnaire=questionnaire,
        submission__evaluator_type="self", submission__status="draft"
    ).exists()
    return {
        "questionnaire_id": questionnaire.pk,
        "employee_id": questionnaire.employee_id,
        "review_cycle_id": questionnaire.review_cycle_id,
        "status": questionnaire.status,
        "submitted_at": questionnaire.submitted_at,
        "is_published": questionnaire.is_published,
        "published_at": questionnaire.published_at,
        "published_by_id": questionnaire.published_by_id,
        "self_review_submitted": self_review_submitted,
        "is_editable": is_editable,
        "has_draft_responses": has_draft_responses,
        "edit_lock_message": None if is_editable else EDIT_LOCK_MESSAGE,
        "excluded_question_ids": excluded_question_ids,
        "questions": [
            {
                "item_id": item.pk,
                "question_id": item.parameter_id,
                "question_text": item.question_text_snapshot or item.parameter.question_text,
                "question_type": item.question_type_snapshot or item.parameter.question_type,
                "max_marks": item.max_marks_snapshot or item.parameter.max_marks,
                "category": item.parameter.category.name if item.parameter.category_id else "Uncategorised",
                "category_type": item.category_type_snapshot or (
                    item.parameter.category.category_type if item.parameter.category_id else None
                ),
                "weightage": item.weightage,
                "order": item.order,
                "source": item.source
            }
            for item in items
        ]
    }