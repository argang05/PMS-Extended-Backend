import json
from django.db import transaction
from django.db.models import Q, Value, CharField
from django.db.models.functions import Concat, Coalesce
from django.shortcuts import get_object_or_404
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from rest_framework.exceptions import ValidationError
from employees.models import Employee
from evaluations.models import (
    QuestionCategory, QuestionParameter, QuestionDepartmentAssignment,
    QuestionEmployeeAssignment, QuestionEmployeeExclusion,
    EmployeeQuestionnaire, EmployeeQuestionnaireItem
)
from evaluations.questionnaire_builder import (
    QuestionnaireBuilderError, _editable, _sync_locked, _question_targets_employee
)
from admin_console.serializers import (
    QuestionCategorySerializer, QuestionParameterListSerializer,
    QuestionParameterCreateUpdateSerializer
)

def is_admin_user(user):
    return str(getattr(user, "employee_number", "")) == "100607"

TARGET_MODES = {"manual", "legacy", "all", "departments", "employees", "combined"}
TARGET_FIELDS = {"departments", "employee_ids", "excluded_employee_ids"}

def parse_json_value(value, field):
    if isinstance(value, str):
        try:
            return json.loads(value)
        except (ValueError, TypeError):
            raise ValidationError({field: "Invalid JSON value."})
    return value

def normalize_departments(value):
    value = parse_json_value(value, "departments")
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValidationError({"departments": "Expected a list of department names."})
    departments, seen = [], set()
    for department in value:
        if not isinstance(department, str):
            raise ValidationError({"departments": "Every department must be a string."})
        department = department.strip()
        if not department:
            raise ValidationError({"departments": "Department names cannot be empty."})
        if len(department) > 150:
            raise ValidationError({"departments": "Department names cannot exceed 150 characters."})
        if department.casefold() not in seen:
            departments.append(department)
            seen.add(department.casefold())
    return departments

def normalize_employee_ids(value, field):
    value = parse_json_value(value, field)
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValidationError({field: "Expected a list of employee IDs."})
    employee_ids = []
    for employee_id in value:
        if isinstance(employee_id, bool):
            raise ValidationError({field: "Employee IDs must be positive integers."})
        try:
            employee_id = int(employee_id)
        except (ValueError, TypeError, OverflowError):
            raise ValidationError({field: "Employee IDs must be positive integers."})
        if employee_id <= 0:
            raise ValidationError({field: "Employee IDs must be positive integers."})
        if employee_id not in employee_ids:
            employee_ids.append(employee_id)
    return employee_ids

def validate_employees(employee_ids, field):
    if not employee_ids:
        return
    existing = set(Employee.objects.filter(id__in=employee_ids).values_list("id", flat=True))
    missing = sorted(set(employee_ids) - existing)
    if missing:
        raise ValidationError({field: f"Employee IDs not found: {missing}"})

def normalize_targeting(data, instance=None):
    mode = data.get("target_mode", instance.target_mode if instance else "manual")
    if mode not in TARGET_MODES:
        raise ValidationError({"target_mode": "Invalid targeting mode."})

    current_departments = list(
        instance.department_assignments.values_list("department", flat=True)
    ) if instance else []
    current_employees = list(
        instance.employee_assignments.values_list("employee_id", flat=True)
    ) if instance else []
    current_exclusions = list(
        instance.employee_exclusions.values_list("employee_id", flat=True)
    ) if instance else []

    # Switching to Question Bank Only clears previous automatic targeting.
    # Explicitly supplied non-empty assignment lists are rejected.
    if mode == "manual":
        for field in TARGET_FIELDS:
            if field in data:
                values = normalize_departments(data[field]) if field == "departments" else normalize_employee_ids(data[field], field)
                if values:
                    raise ValidationError({
                        field: "Question Bank Only does not accept automatic targeting assignments."
                    })
        return {
            "target_mode": "manual",
            "departments": [],
            "employee_ids": [],
            "excluded_employee_ids": []
        }

    departments = normalize_departments(
        data["departments"] if "departments" in data else current_departments
    )
    employee_ids = normalize_employee_ids(
        data["employee_ids"] if "employee_ids" in data else current_employees,
        "employee_ids"
    )
    excluded_ids = normalize_employee_ids(
        data["excluded_employee_ids"] if "excluded_employee_ids" in data else current_exclusions,
        "excluded_employee_ids"
    )

    if mode == "legacy":
        if departments or employee_ids or excluded_ids:
            raise ValidationError({
                "target_mode": "Legacy targeting uses the existing department field. Remove assignment lists or select another targeting mode."
            })
    elif mode == "all":
        if departments or employee_ids:
            raise ValidationError({
                "target_mode": "All-employees targeting does not accept department or employee assignments."
            })
    elif mode == "departments":
        if not departments:
            raise ValidationError({"departments": "Select at least one department."})
        if employee_ids:
            raise ValidationError({"employee_ids": "Employee assignments require employees or combined mode."})
    elif mode == "employees":
        if not employee_ids:
            raise ValidationError({"employee_ids": "Select at least one employee."})
        if departments:
            raise ValidationError({"departments": "Department assignments require departments or combined mode."})
    elif mode == "combined":
        if not departments and not employee_ids:
            raise ValidationError({
                "target_mode": "Combined targeting requires at least one department or employee."
            })

    validate_employees(employee_ids, "employee_ids")
    validate_employees(excluded_ids, "excluded_employee_ids")

    return {
        "target_mode": mode,
        "departments": departments,
        "employee_ids": employee_ids,
        "excluded_employee_ids": excluded_ids
    }

def save_targeting(question, targeting):
    QuestionDepartmentAssignment.objects.filter(parameter=question).delete()
    QuestionEmployeeAssignment.objects.filter(parameter=question).delete()
    QuestionEmployeeExclusion.objects.filter(parameter=question).delete()
    QuestionDepartmentAssignment.objects.bulk_create([
        QuestionDepartmentAssignment(parameter=question, department=department)
        for department in targeting["departments"]
    ])
    QuestionEmployeeAssignment.objects.bulk_create([
        QuestionEmployeeAssignment(parameter=question, employee_id=employee_id)
        for employee_id in targeting["employee_ids"]
    ])
    QuestionEmployeeExclusion.objects.bulk_create([
        QuestionEmployeeExclusion(parameter=question, employee_id=employee_id)
        for employee_id in targeting["excluded_employee_ids"]
    ])

def serialize_question(question):
    data = QuestionParameterListSerializer(question).data
    data["departments"] = list(question.department_assignments.values_list("department", flat=True))
    data["employee_ids"] = list(question.employee_assignments.values_list("employee_id", flat=True))
    data["excluded_employee_ids"] = list(question.employee_exclusions.values_list("employee_id", flat=True))
    return data

def question_queryset():
    return QuestionParameter.objects.select_related("category").prefetch_related(
        "department_assignments", "employee_assignments", "employee_exclusions"
    )

def get_targeted_employees(mode, departments, employee_ids, excluded_ids, legacy_department=None):
    queryset = Employee.objects.all()
    if mode == "manual":
        return queryset.none()
    if mode == "legacy":
        if legacy_department:
            queryset = queryset.filter(department__iexact=legacy_department)
    elif mode == "departments":
        condition = Q()
        for department in departments:
            condition |= Q(department__iexact=department)
        queryset = queryset.filter(condition)
    elif mode == "employees":
        queryset = queryset.filter(id__in=employee_ids)
    elif mode == "combined":
        condition = Q(id__in=employee_ids)
        for department in departments:
            condition |= Q(department__iexact=department)
        queryset = queryset.filter(condition)
    if excluded_ids:
        queryset = queryset.exclude(id__in=excluded_ids)
    return queryset.distinct().order_by("id")

def synchronize_questionnaires(question_ids):
    """
    Synchronize editable questionnaires affected by Question Bank changes.
    Preserves manual include/exclude customizations and never creates
    new questionnaire records or modifies published/submitted questionnaires.
    """
    question_ids = set(question_ids)
    if not question_ids:
        return {"synchronized": 0, "skipped": 0, "created": 0, "removed": 0}

    questions = list(question_queryset().filter(id__in=question_ids))
    existing_item_employee_ids = set(
        EmployeeQuestionnaireItem.objects.filter(
            parameter_id__in=question_ids
        ).values_list("employee_questionnaire__employee_id", flat=True)
    )

    questionnaires = (
        EmployeeQuestionnaire.objects.select_for_update()
        .select_related("employee", "review_cycle")
        .filter(status="draft", is_published=False)
        .order_by("id")
    )

    synchronized = skipped = created = removed = 0
    for questionnaire in questionnaires:
        employee = questionnaire.employee
        currently_targeted = any(
            question.is_active and _question_targets_employee(question, employee)
            for question in questions
        )
        previously_included = employee.pk in existing_item_employee_ids
        if not currently_targeted and not previously_included:
            continue
        try:
            _editable(questionnaire)
        except QuestionnaireBuilderError:
            skipped += 1
            continue
        result = _sync_locked(questionnaire)
        synchronized += 1
        created += result["created"]
        removed += result["removed"]

    return {
        "synchronized": synchronized,
        "skipped": skipped,
        "created": created,
        "removed": removed
    }

def create_question(data):
    if not isinstance(data, dict):
        raise ValidationError({"question": "Each question must be a JSON object."})
    payload = dict(data)
    payload.setdefault("question_type", "objective")
    payload.setdefault("default_weightage", 1)
    payload.setdefault("max_marks", 10)
    payload.setdefault("target_mode", "manual")

    targeting = normalize_targeting(payload)
    serializer = QuestionParameterCreateUpdateSerializer(data=payload)
    serializer.is_valid(raise_exception=True)
    question = serializer.save()
    save_targeting(question, targeting)
    return question

class AdminQuestionCategoryListView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        if not is_admin_user(request.user):
            return Response({"error": "Not authorized"}, status=403)
        categories = QuestionCategory.objects.all().order_by("category_type", "name")
        serializer = QuestionCategorySerializer(categories, many=True)
        return Response({"results": serializer.data})

class AdminQuestionListCreateView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        if not is_admin_user(request.user):
            return Response({"error": "Not authorized"}, status=403)

        search = request.query_params.get("search", "").strip()
        category_type = request.query_params.get("category_type", "").strip()
        department = request.query_params.get("department", "").strip()
        is_active = request.query_params.get("is_active", "").strip()
        question_type = request.query_params.get("question_type", "").strip()
        target_mode = request.query_params.get("target_mode", "").strip()
        queryset = question_queryset().order_by("-id")

        if search:
            queryset = queryset.filter(
                Q(question_text__icontains=search) |
                Q(category__name__icontains=search) |
                Q(department__icontains=search) |
                Q(department_assignments__department__icontains=search)
            )
        if category_type:
            queryset = queryset.filter(category__category_type=category_type)
        if department:
            if department.lower() == "common":
                queryset = queryset.filter(department__isnull=True)
            else:
                queryset = queryset.filter(
                    Q(department=department) |
                    Q(department_assignments__department=department)
                )
        if is_active.lower() in ["true", "false"]:
            queryset = queryset.filter(is_active=is_active.lower() == "true")
        if question_type:
            queryset = queryset.filter(question_type=question_type)
        if target_mode:
            queryset = queryset.filter(target_mode=target_mode)

        results = [serialize_question(question) for question in queryset.distinct()]
        return Response({"results": results})

    @transaction.atomic
    def post(self, request):
        if not is_admin_user(request.user):
            return Response({"error": "Not authorized"}, status=403)
        question = create_question(request.data)
        sync_result = synchronize_questionnaires([question.pk])
        return Response({
            "message": "Question created successfully.",
            "question": serialize_question(question),
            "questionnaire_sync": sync_result
        }, status=201)

class AdminQuestionDetailView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, question_id):
        if not is_admin_user(request.user):
            return Response({"error": "Not authorized"}, status=403)
        question = get_object_or_404(question_queryset(), id=question_id)
        return Response({"question": serialize_question(question)})

    @transaction.atomic
    def put(self, request, question_id):
        if not is_admin_user(request.user):
            return Response({"error": "Not authorized"}, status=403)

        question = get_object_or_404(
            QuestionParameter.objects.select_for_update(), id=question_id
        )
        serializer = QuestionParameterCreateUpdateSerializer(
            question, data=request.data, partial=True
        )
        serializer.is_valid(raise_exception=True)

        targeting_changed = (
            "target_mode" in request.data or
            any(field in request.data for field in TARGET_FIELDS)
        )
        targeting = normalize_targeting(request.data, question) if targeting_changed else None
        updated_question = serializer.save()

        if targeting is not None:
            save_targeting(updated_question, targeting)

        sync_result = synchronize_questionnaires([updated_question.pk])
        return Response({
            "message": "Question updated successfully.",
            "question": serialize_question(updated_question),
            "questionnaire_sync": sync_result
        })

    @transaction.atomic
    def delete(self, request, question_id):
        if not is_admin_user(request.user):
            return Response({"error": "Not authorized"}, status=403)
        question = get_object_or_404(
            QuestionParameter.objects.select_for_update(), id=question_id
        )
        question.is_active = False
        question.save(update_fields=["is_active"])
        sync_result = synchronize_questionnaires([question.pk])
        return Response({
            "message": "Question deactivated successfully.",
            "questionnaire_sync": sync_result
        })

class AdminQuestionBulkCreateView(APIView):
    permission_classes = [IsAuthenticated]

    @transaction.atomic
    def post(self, request):
        if not is_admin_user(request.user):
            return Response({"error": "Not authorized"}, status=403)

        payload = request.data
        if isinstance(payload, dict):
            questions = payload.get("questions")
        elif isinstance(payload, list):
            questions = payload
        else:
            return Response({
                "error": "Provide a JSON array or an object containing a questions array."
            }, status=400)

        questions = parse_json_value(questions, "questions")
        if not isinstance(questions, list) or not questions:
            return Response({
                "error": "questions must be a non-empty JSON array."
            }, status=400)

        created = []
        for index, data in enumerate(questions):
            try:
                question = create_question(data)
                created.append(serialize_question(question))
            except ValidationError as exc:
                raise ValidationError({
                    "question_index": index,
                    "question_number": index + 1,
                    "details": exc.detail
                })

        question_ids = [question["id"] for question in created]
        sync_result = synchronize_questionnaires(question_ids)
        return Response({
            "message": f"{len(created)} questions created successfully.",
            "created_count": len(created),
            "questions": created,
            "questionnaire_sync": sync_result
        }, status=201)

class AdminQuestionTargetingPreviewView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        if not is_admin_user(request.user):
            return Response({"error": "Not authorized"}, status=403)

        question_id = request.data.get("question_id")
        question = None
        if question_id is not None:
            question = get_object_or_404(QuestionParameter, id=question_id)

        targeting = normalize_targeting(request.data, question)
        legacy_department = request.data.get(
            "department", question.department if question else None
        )
        employees = get_targeted_employees(
            targeting["target_mode"],
            targeting["departments"],
            targeting["employee_ids"],
            targeting["excluded_employee_ids"],
            legacy_department
        )
        results = [{
            "id": employee.id,
            "employee_number": employee.employee_number,
            "full_name": getattr(employee, "full_name", str(employee)),
            "department": employee.department
        } for employee in employees]

        return Response({
            "target_mode": targeting["target_mode"],
            "departments": targeting["departments"],
            "employee_ids": targeting["employee_ids"],
            "excluded_employee_ids": targeting["excluded_employee_ids"],
            "total_employees": len(results),
            "employees": results
        })

class AdminQuestionTargetingOptionsView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        if not is_admin_user(request.user):
            return Response({"error": "Not authorized"}, status=403)

        search = request.query_params.get("search", "").strip()
        department = request.query_params.get("department", "").strip()

        departments = list(
            Employee.objects.exclude(department__isnull=True)
            .exclude(department="")
            .order_by("department")
            .values_list("department", flat=True)
            .distinct()
        )
        employees = Employee.objects.all().order_by("id")

        if search:
            employees = employees.annotate(
                searchable_full_name=Concat(
                    "first_name", Value(" "),
                    Coalesce("last_name", Value("")),
                    output_field=CharField()
                )
            ).filter(
                Q(employee_number__icontains=search) |
                Q(first_name__icontains=search) |
                Q(last_name__icontains=search) |
                Q(searchable_full_name__icontains=search) |
                Q(department__icontains=search)
            )
        if department:
            employees = employees.filter(department__iexact=department)

        results = [{
            "id": employee.id,
            "employee_number": employee.employee_number,
            "full_name": employee.full_name,
            "department": employee.department
        } for employee in employees[:100]]

        return Response({
            "departments": departments,
            "employees": results,
            "employee_limit": 100
        })