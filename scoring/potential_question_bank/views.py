from django.db import transaction
from django.db.models import Count, Q
from django.shortcuts import get_object_or_404
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status
from rest_framework.exceptions import ValidationError
from scoring.models import (
    PotentialParameter,
    PotentialQuestionCategory,
)
from .permissions import IsPotentialQuestionBankAdmin
from .serializers import (
    PotentialCategorySerializer,
    PotentialEmployeeSerializer,
    PotentialQuestionSerializer,
    PotentialQuestionWriteSerializer,
    PotentialTargetingSerializer,
    PotentialBulkCreateSerializer,
    PotentialBulkConfigureSerializer,
    PotentialBulkDeleteSerializer,
)
from .services import (
    question_bank_queryset,
    eligible_employee_queryset,
    resolve_target_employees,
    resolve_question_employees,
    create_question,
    bulk_create_questions,
    update_question,
    bulk_configure_questions,
    delete_questions,
)


class PotentialAdminAPIView(APIView):
    permission_classes = [IsPotentialQuestionBankAdmin]


class PotentialCategoryListCreateView(PotentialAdminAPIView):
    def get(self, request):
        categories = PotentialQuestionCategory.objects.annotate(
            question_count=Count("parameters")
        ).order_by("name")

        return Response(
            PotentialCategorySerializer(categories, many=True).data
        )

    def post(self, request):
        serializer = PotentialCategorySerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        category = serializer.save()
        category.question_count = 0

        return Response({
            "message": "Potential category created successfully.",
            "category": PotentialCategorySerializer(category).data
        }, status=status.HTTP_201_CREATED)


class PotentialCategoryDetailView(PotentialAdminAPIView):
    def patch(self, request, category_id):
        category = get_object_or_404(
            PotentialQuestionCategory, pk=category_id
        )
        serializer = PotentialCategorySerializer(
            category, data=request.data, partial=True
        )
        serializer.is_valid(raise_exception=True)
        category = serializer.save()
        category.question_count = category.parameters.count()

        return Response({
            "message": "Potential category updated successfully.",
            "category": PotentialCategorySerializer(category).data
        })

    def put(self, request, category_id):
        return self.patch(request, category_id)

    @transaction.atomic
    def delete(self, request, category_id):
        category = get_object_or_404(
            PotentialQuestionCategory.objects.select_for_update(),
            pk=category_id
        )

        if category.parameters.exists():
            raise ValidationError({
                "message": (
                    "This category contains questions. Reassign or remove "
                    "its questions before deleting the category."
                )
            })

        category.delete()

        return Response({
            "message": "Potential category deleted successfully.",
            "deleted_id": category_id
        })


class PotentialQuestionListCreateView(PotentialAdminAPIView):
    def get(self, request):
        queryset = question_bank_queryset()

        search = request.query_params.get("search", "").strip()
        category = request.query_params.get("category", "")
        question_type = request.query_params.get("question_type", "")
        target_mode = request.query_params.get("target_mode", "")
        active = request.query_params.get("is_active", "").lower()
        department = request.query_params.get("department", "").strip()
        configured = request.query_params.get("configured", "").lower()

        if search:
            queryset = queryset.filter(
                Q(question_text__icontains=search)
                | Q(category__name__icontains=search)
            )

        if category == "uncategorized":
            queryset = queryset.filter(category__isnull=True)
        elif category:
            if not category.isdigit():
                raise ValidationError({"category": "Invalid category ID."})
            queryset = queryset.filter(category_id=int(category))

        if question_type:
            queryset = queryset.filter(question_type=question_type)

        if target_mode:
            queryset = queryset.filter(target_mode=target_mode)

        if active in ("true", "false"):
            queryset = queryset.filter(is_active=active == "true")

        if department:
            queryset = queryset.filter(
                department_assignments__department=department
            ).distinct()

        if configured == "true":
            queryset = queryset.filter(
                category__isnull=False
            ).exclude(target_mode="manual")
        elif configured == "false":
            queryset = queryset.filter(
                Q(category__isnull=True) | Q(target_mode="manual")
            )

        return Response(
            PotentialQuestionSerializer(queryset, many=True).data
        )

    def post(self, request):
        if isinstance(request.data, list):
            serializer = PotentialBulkCreateSerializer(data={
                "questions": request.data
            })
            serializer.is_valid(raise_exception=True)
            questions = bulk_create_questions(
                serializer.validated_data["questions"]
            )
            return Response({
                "message": f"{len(questions)} Potential questions created successfully.",
                "created_count": len(questions),
                "questions": PotentialQuestionSerializer(
                    questions, many=True
                ).data
            }, status=status.HTTP_201_CREATED)

        serializer = PotentialQuestionWriteSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        question = create_question(serializer.validated_data)

        return Response({
            "message": "Potential question created successfully.",
            "question": PotentialQuestionSerializer(question).data
        }, status=status.HTTP_201_CREATED)


class PotentialQuestionDetailView(PotentialAdminAPIView):
    def get(self, request, question_id):
        question = get_object_or_404(
            question_bank_queryset(), pk=question_id
        )
        return Response(PotentialQuestionSerializer(question).data)

    def patch(self, request, question_id):
        get_object_or_404(PotentialParameter, pk=question_id)

        question = update_question(question_id, request.data)

        return Response({
            "message": "Potential question updated successfully.",
            "question": PotentialQuestionSerializer(question).data
        })

    def put(self, request, question_id):
        return self.patch(request, question_id)

    def delete(self, request, question_id):
        get_object_or_404(PotentialParameter, pk=question_id)
        deleted_ids = delete_questions([question_id])

        return Response({
            "message": "Potential question deleted successfully.",
            "deleted_id": deleted_ids[0]
        })


class PotentialQuestionBulkCreateView(PotentialAdminAPIView):
    def post(self, request):
        payload = (
            {"questions": request.data}
            if isinstance(request.data, list)
            else request.data
        )

        serializer = PotentialBulkCreateSerializer(data=payload)
        serializer.is_valid(raise_exception=True)

        questions = bulk_create_questions(
            serializer.validated_data["questions"]
        )

        return Response({
            "message": f"{len(questions)} Potential questions created successfully.",
            "created_count": len(questions),
            "questions": PotentialQuestionSerializer(
                questions, many=True
            ).data
        }, status=status.HTTP_201_CREATED)


class PotentialQuestionBulkConfigureView(PotentialAdminAPIView):
    def post(self, request):
        serializer = PotentialBulkConfigureSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        questions = bulk_configure_questions(
            serializer.validated_data["question_ids"],
            serializer.validated_data["configuration"]
        )

        return Response({
            "message": f"{len(questions)} Potential questions configured successfully.",
            "updated_count": len(questions),
            "questions": PotentialQuestionSerializer(
                questions, many=True
            ).data
        })


class PotentialQuestionBulkDeleteView(PotentialAdminAPIView):
    def post(self, request):
        serializer = PotentialBulkDeleteSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        deleted_ids = delete_questions(
            serializer.validated_data["question_ids"]
        )

        return Response({
            "message": f"{len(deleted_ids)} Potential questions deleted successfully.",
            "deleted_count": len(deleted_ids),
            "deleted_ids": deleted_ids
        })


class PotentialTargetingOptionsView(PotentialAdminAPIView):
    def get(self, request):
        employees = eligible_employee_queryset()

        departments = list(
            employees.exclude(department__isnull=True)
            .exclude(department="")
            .order_by("department")
            .values_list("department", flat=True)
            .distinct()
        )

        return Response({
            "departments": departments,
            "employees": PotentialEmployeeSerializer(
                employees, many=True
            ).data,
            "employee_count": employees.count()
        })


class PotentialTargetingPreviewView(PotentialAdminAPIView):
    def post(self, request):
        serializer = PotentialTargetingSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        employees = resolve_target_employees(
            **serializer.validated_data
        )
        employee_list = list(employees)

        return Response({
            "target_mode": serializer.validated_data["target_mode"],
            "employee_count": len(employee_list),
            "employees": PotentialEmployeeSerializer(
                employee_list, many=True
            ).data
        })


class PotentialQuestionTargetingPreviewView(PotentialAdminAPIView):
    def get(self, request, question_id):
        question = get_object_or_404(
            question_bank_queryset(), pk=question_id
        )

        employees = list(resolve_question_employees(question))

        return Response({
            "question_id": question.pk,
            "target_mode": question.target_mode,
            "employee_count": len(employees),
            "employees": PotentialEmployeeSerializer(
                employees, many=True
            ).data
        })


class PotentialQuestionBankStatsView(PotentialAdminAPIView):
    def get(self, request):
        stats = PotentialParameter.objects.aggregate(
            total=Count("id"),
            active=Count("id", filter=Q(is_active=True)),
            inactive=Count("id", filter=Q(is_active=False)),
            objective=Count("id", filter=Q(question_type="objective")),
            descriptive=Count("id", filter=Q(question_type="descriptive")),
            uncategorized=Count("id", filter=Q(category__isnull=True)),
            manual=Count("id", filter=Q(target_mode="manual")),
            legacy=Count("id", filter=Q(target_mode="legacy")),
            configured=Count(
                "id",
                filter=Q(category__isnull=False) & ~Q(target_mode="manual")
            )
        )

        return Response({
            **stats,
            "category_count": PotentialQuestionCategory.objects.count()
        })