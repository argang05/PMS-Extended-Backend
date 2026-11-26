from django.contrib.auth import get_user_model
from django.db import transaction
from django.db.models import Q, Exists, OuterRef, Prefetch
from django.db.models.deletion import ProtectedError
from rest_framework.exceptions import ValidationError
from scoring.models import (
    PotentialParameter, PotentialDepartmentAssignment, PotentialEmployeeAssignment,
    PotentialEmployeeExclusion, EmployeePotentialQuestionnaireItem,
    EmployeePotentialQuestionnaireCustomization, EmployeePotentialResponse
)
from .serializers import PotentialQuestionWriteSerializer

Employee = get_user_model()
TARGETING_FIELDS = {"departments", "employee_ids", "excluded_employee_ids"}
STRUCTURAL_FIELDS = {"question_text", "question_type", "max_marks", "weightage"}


def question_bank_queryset():
    used_items = EmployeePotentialQuestionnaireItem.objects.filter(parameter_id=OuterRef("pk"))
    published_items = used_items.filter(assessment__is_published=True)
    return PotentialParameter.objects.select_related("category").prefetch_related(
        "department_assignments",
        Prefetch("employee_assignments", queryset=PotentialEmployeeAssignment.objects.select_related("employee")),
        Prefetch("employee_exclusions", queryset=PotentialEmployeeExclusion.objects.select_related("employee"))
    ).annotate(used_flag=Exists(used_items), published_used_flag=Exists(published_items)).order_by("-id")


def eligible_employee_queryset():
    return Employee.objects.filter(is_active=True).order_by("first_name", "last_name", "pk")


def resolve_target_employees(target_mode, departments=None, employee_ids=None, excluded_employee_ids=None):
    departments = departments or []
    employee_ids = employee_ids or []
    excluded_employee_ids = excluded_employee_ids or []
    queryset = eligible_employee_queryset()
    if target_mode == "manual":
        return queryset.none()
    if target_mode in ("all", "legacy"):
        pass
    elif target_mode == "departments":
        queryset = queryset.filter(department__in=departments)
    elif target_mode == "employees":
        queryset = queryset.filter(pk__in=employee_ids)
    elif target_mode == "combined":
        queryset = queryset.filter(Q(department__in=departments) | Q(pk__in=employee_ids))
    else:
        return queryset.none()
    if excluded_employee_ids:
        queryset = queryset.exclude(pk__in=excluded_employee_ids)
    return queryset.distinct()


def resolve_question_employees(question):
    if not question.is_active:
        return eligible_employee_queryset().none()
    return resolve_target_employees(
        target_mode=question.target_mode,
        departments=[row.department for row in question.department_assignments.all()],
        employee_ids=[row.employee_id for row in question.employee_assignments.all()],
        excluded_employee_ids=[row.employee_id for row in question.employee_exclusions.all()]
    )


def resolve_automatic_questions(employee):
    if not employee.is_active:
        return PotentialParameter.objects.none()
    employee_assignment = PotentialEmployeeAssignment.objects.filter(parameter_id=OuterRef("pk"), employee_id=employee.pk)
    department_assignment = PotentialDepartmentAssignment.objects.filter(parameter_id=OuterRef("pk"), department=employee.department)
    employee_exclusion = PotentialEmployeeExclusion.objects.filter(parameter_id=OuterRef("pk"), employee_id=employee.pk)
    return PotentialParameter.objects.filter(is_active=True).annotate(
        employee_targeted=Exists(employee_assignment),
        department_targeted=Exists(department_assignment),
        employee_excluded=Exists(employee_exclusion)
    ).filter(employee_excluded=False).filter(
        Q(target_mode__in=["all", "legacy"])
        | Q(target_mode="departments", department_targeted=True)
        | Q(target_mode="employees", employee_targeted=True)
        | (Q(target_mode="combined") & (Q(employee_targeted=True) | Q(department_targeted=True)))
    ).select_related("category").order_by("id")


def replace_targeting(question, targeting):
    departments = targeting.get("departments", [])
    employee_ids = targeting.get("employee_ids", [])
    excluded_ids = targeting.get("excluded_employee_ids", [])
    question.department_assignments.all().delete()
    question.employee_assignments.all().delete()
    question.employee_exclusions.all().delete()
    PotentialDepartmentAssignment.objects.bulk_create([
        PotentialDepartmentAssignment(parameter=question, department=department)
        for department in departments
    ])
    PotentialEmployeeAssignment.objects.bulk_create([
        PotentialEmployeeAssignment(parameter=question, employee_id=employee_id)
        for employee_id in employee_ids
    ])
    PotentialEmployeeExclusion.objects.bulk_create([
        PotentialEmployeeExclusion(parameter=question, employee_id=employee_id)
        for employee_id in excluded_ids
    ])


def split_question_data(validated_data):
    data = dict(validated_data)
    targeting = {field: data.pop(field) for field in TARGETING_FIELDS if field in data}
    return data, targeting


@transaction.atomic
def create_question(validated_data):
    data, targeting = split_question_data(validated_data)
    question = PotentialParameter.objects.create(**data)
    replace_targeting(question, targeting)
    return question_bank_queryset().get(pk=question.pk)


@transaction.atomic
def bulk_create_questions(validated_questions):
    model_objects, targeting_rows = [], []
    for validated_data in validated_questions:
        data, targeting = split_question_data(validated_data)
        model_objects.append(PotentialParameter(**data))
        targeting_rows.append(targeting)
    questions = PotentialParameter.objects.bulk_create(model_objects, batch_size=500)
    department_rows, employee_rows, exclusion_rows = [], [], []
    for question, targeting in zip(questions, targeting_rows):
        department_rows.extend([
            PotentialDepartmentAssignment(parameter_id=question.pk, department=department)
            for department in targeting.get("departments", [])
        ])
        employee_rows.extend([
            PotentialEmployeeAssignment(parameter_id=question.pk, employee_id=employee_id)
            for employee_id in targeting.get("employee_ids", [])
        ])
        exclusion_rows.extend([
            PotentialEmployeeExclusion(parameter_id=question.pk, employee_id=employee_id)
            for employee_id in targeting.get("excluded_employee_ids", [])
        ])
    PotentialDepartmentAssignment.objects.bulk_create(department_rows, batch_size=1000)
    PotentialEmployeeAssignment.objects.bulk_create(employee_rows, batch_size=1000)
    PotentialEmployeeExclusion.objects.bulk_create(exclusion_rows, batch_size=1000)
    ids = [question.pk for question in questions]
    result_map = {question.pk: question for question in question_bank_queryset().filter(pk__in=ids)}
    return [result_map[pk] for pk in ids]


def validate_legacy_question_edit(question, data):
    changed_fields = sorted([
        field for field in STRUCTURAL_FIELDS
        if field in data and getattr(question, field) != data[field]
    ])
    if not changed_fields:
        return
    has_legacy_responses = EmployeePotentialResponse.objects.filter(
        parameter_id=question.pk, questionnaire_item__isnull=True
    ).exists()
    if has_legacy_responses:
        raise ValidationError({
            "question": "This question has historical responses without questionnaire snapshots. Its scoring definition cannot be changed.",
            "question_id": question.pk,
            "protected_fields": changed_fields
        })
    if EmployeePotentialQuestionnaireItem.objects.filter(parameter_id=question.pk, assessment__is_published=True).exists():
        raise ValidationError({
            "question": "This question is used in a published questionnaire. Its scoring definition cannot be changed.",
            "question_id": question.pk,
            "protected_fields": changed_fields
        })


def apply_question_update(question, validated_data):
    data, targeting = split_question_data(validated_data)
    validate_legacy_question_edit(question, data)
    changed_fields = []
    for field, value in data.items():
        if getattr(question, field) != value:
            setattr(question, field, value)
            changed_fields.append(field)
    if changed_fields or targeting:
        question.save(update_fields=[*changed_fields, "updated_at"])
    if targeting:
        replace_targeting(question, targeting)
    return question


@transaction.atomic
def update_question(question_id, payload):
    question = PotentialParameter.objects.select_for_update().get(pk=question_id)
    serializer = PotentialQuestionWriteSerializer(instance=question, data=payload, partial=True)
    serializer.is_valid(raise_exception=True)
    apply_question_update(question, serializer.validated_data)
    return question_bank_queryset().get(pk=question.pk)


@transaction.atomic
def bulk_configure_questions(question_ids, configuration):
    question_ids = sorted(set(question_ids))
    questions = list(PotentialParameter.objects.select_for_update().filter(pk__in=question_ids).order_by("pk"))
    found_ids = {question.pk for question in questions}
    missing_ids = sorted(set(question_ids) - found_ids)
    if missing_ids:
        raise ValidationError({"question_ids": f"Questions not found: {missing_ids}"})
    validated_rows = []
    for question in questions:
        serializer = PotentialQuestionWriteSerializer(instance=question, data=configuration, partial=True)
        if not serializer.is_valid():
            raise ValidationError({"question_id": question.pk, "errors": serializer.errors})
        data, _ = split_question_data(serializer.validated_data)
        validate_legacy_question_edit(question, data)
        validated_rows.append((question, serializer.validated_data))
    for question, validated_data in validated_rows:
        apply_question_update(question, validated_data)
    return list(question_bank_queryset().filter(pk__in=question_ids))


def question_deletion_blockers(question_ids):
    used_item_ids = set(EmployeePotentialQuestionnaireItem.objects.filter(parameter_id__in=question_ids).values_list("parameter_id", flat=True))
    response_ids = set(EmployeePotentialResponse.objects.filter(parameter_id__in=question_ids).values_list("parameter_id", flat=True))
    customization_ids = set(EmployeePotentialQuestionnaireCustomization.objects.filter(parameter_id__in=question_ids).values_list("parameter_id", flat=True))
    return sorted(used_item_ids | response_ids | customization_ids)


@transaction.atomic
def delete_questions(question_ids):
    question_ids = sorted(set(question_ids))
    questions = list(PotentialParameter.objects.select_for_update().filter(pk__in=question_ids).order_by("pk"))
    found_ids = {question.pk for question in questions}
    missing_ids = sorted(set(question_ids) - found_ids)
    if missing_ids:
        raise ValidationError({"question_ids": f"Questions not found: {missing_ids}"})
    blocked_ids = question_deletion_blockers(question_ids)
    if blocked_ids:
        raise ValidationError({
            "message": "Questions used in questionnaires or historical evaluations cannot be deleted. Deactivate them instead.",
            "blocked_question_ids": blocked_ids
        })
    try:
        PotentialParameter.objects.filter(pk__in=question_ids).delete()
    except ProtectedError:
        raise ValidationError({"message": "One or more questions are protected by existing assessments."})
    return question_ids