from django.contrib.auth import get_user_model
from django.db.models import Q
from django.shortcuts import get_object_or_404
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status
from rest_framework.exceptions import ValidationError
from evaluations.models import ReviewCycle
from scoring.models import EmployeePotentialAssessment
from scoring.potential_question_bank.permissions import IsPotentialQuestionBankAdmin
from scoring.potential_question_bank.serializers import PotentialQuestionSerializer, PotentialEmployeeSerializer
from .serializers import (
    PotentialQuestionnaireCreateSerializer, PotentialQuestionnaireListSerializer,
    PotentialQuestionnaireDetailSerializer, PotentialDraftSaveSerializer,
    PotentialRevisionSerializer, PotentialQuestionnaireCopySerializer,
    PotentialQuestionnairePublishSerializer
)
from .services import (
    questionnaire_queryset, get_questionnaire_detail, create_questionnaire,
    save_questionnaire_draft, synchronize_questionnaire, reset_questionnaire,
    copy_questionnaire, publish_questionnaire, get_available_questions,
    PotentialResetRequired
)

Employee = get_user_model()

class PotentialQuestionnaireAdminView(APIView):
    permission_classes = [IsPotentialQuestionBankAdmin]

def questionnaire_response(message, assessment, response_status=status.HTTP_200_OK):
    return Response({
        "message": message,
        "questionnaire": PotentialQuestionnaireDetailSerializer(assessment).data
    }, status=response_status)

class PotentialQuestionnaireOptionsView(PotentialQuestionnaireAdminView):
    def get(self, request):
        employees = Employee.objects.filter(is_active=True).order_by("first_name", "last_name", "pk")
        cycles = ReviewCycle.objects.all().order_by("-start_date", "-id")
        return Response({
            "employees": PotentialEmployeeSerializer(employees, many=True).data,
            "review_cycles": list(cycles.values("id", "name", "start_date", "end_date", "is_active"))
        })

class PotentialQuestionnaireListCreateView(PotentialQuestionnaireAdminView):
    def get(self, request):
        queryset = questionnaire_queryset()
        employee_id = request.query_params.get("employee_id")
        cycle_id = request.query_params.get("cycle_id")
        department = request.query_params.get("department", "").strip()
        assessment_status = request.query_params.get("status", "")
        published = request.query_params.get("is_published", "").lower()
        search = request.query_params.get("search", "").strip()
        if employee_id:
            if not employee_id.isdigit():
                raise ValidationError({"employee_id": "Invalid employee ID."})
            queryset = queryset.filter(employee_id=int(employee_id))
        if cycle_id:
            if not cycle_id.isdigit():
                raise ValidationError({"cycle_id": "Invalid review cycle ID."})
            queryset = queryset.filter(review_cycle_id=int(cycle_id))
        if department:
            queryset = queryset.filter(employee__department=department)
        if assessment_status:
            queryset = queryset.filter(status=assessment_status)
        if published in ("true", "false"):
            queryset = queryset.filter(is_published=published == "true")
        if search:
            queryset = queryset.filter(
                Q(employee__first_name__icontains=search)
                | Q(employee__last_name__icontains=search)
                | Q(employee__employee_number__icontains=search)
                | Q(employee__department__icontains=search)
            )
        return Response(PotentialQuestionnaireListSerializer(queryset, many=True).data)

    def post(self, request):
        serializer = PotentialQuestionnaireCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        assessment, created = create_questionnaire(
            employee_id=serializer.validated_data["employee_id"],
            review_cycle_id=serializer.validated_data["review_cycle_id"],
            admin=request.user
        )
        return questionnaire_response(
            "Potential questionnaire initialized successfully." if created else "Existing Potential questionnaire loaded successfully.",
            assessment, status.HTTP_201_CREATED if created else status.HTTP_200_OK
        )

class PotentialQuestionnaireDetailView(PotentialQuestionnaireAdminView):
    def get(self, request, assessment_id):
        assessment = get_questionnaire_detail(assessment_id)
        return Response(PotentialQuestionnaireDetailSerializer(assessment).data)

class PotentialQuestionnaireAvailableQuestionsView(PotentialQuestionnaireAdminView):
    def get(self, request, assessment_id):
        assessment = get_object_or_404(
            EmployeePotentialAssessment.objects.select_related("employee"), pk=assessment_id
        )
        result = get_available_questions(assessment)
        return Response({
            "questions": PotentialQuestionSerializer(result["questions"], many=True).data,
            "automatic_parameter_ids": result["automatic_parameter_ids"],
            "assigned_parameter_ids": result["assigned_parameter_ids"],
            "excluded_parameter_ids": result["excluded_parameter_ids"]
        })

class PotentialQuestionnaireSaveView(PotentialQuestionnaireAdminView):
    def put(self, request, assessment_id):
        serializer = PotentialDraftSaveSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        assessment = save_questionnaire_draft(
            assessment_id=assessment_id, revision=data["revision"],
            rows=data["items"], excluded_ids=data["excluded_parameter_ids"], admin=request.user
        )
        return questionnaire_response(
            "Customization draft saved successfully. Active evaluations remain unchanged.", assessment
        )

class PotentialQuestionnaireSyncView(PotentialQuestionnaireAdminView):
    def post(self, request, assessment_id):
        serializer = PotentialRevisionSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        assessment = synchronize_questionnaire(
            assessment_id=assessment_id, revision=serializer.validated_data["revision"], admin=request.user
        )
        return questionnaire_response("Customization draft synchronized successfully.", assessment)

class PotentialQuestionnaireResetView(PotentialQuestionnaireAdminView):
    def post(self, request, assessment_id):
        serializer = PotentialRevisionSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        assessment = reset_questionnaire(
            assessment_id=assessment_id, revision=serializer.validated_data["revision"], admin=request.user
        )
        return questionnaire_response(
            "Customization draft reset to automatic configuration. Active evaluations remain unchanged.", assessment
        )

class PotentialQuestionnaireCopyView(PotentialQuestionnaireAdminView):
    def post(self, request, assessment_id):
        serializer = PotentialQuestionnaireCopySerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        assessment = copy_questionnaire(
            assessment_id=assessment_id, source_assessment_id=data["source_assessment_id"],
            revision=data["revision"], copy_weightages=data["copy_weightages"], admin=request.user
        )
        return questionnaire_response("Questionnaire copied into customization draft successfully.", assessment)

class PotentialQuestionnairePublishView(PotentialQuestionnaireAdminView):
    def post(self, request, assessment_id):
        serializer = PotentialQuestionnairePublishSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        try:
            assessment = publish_questionnaire(
                assessment_id=assessment_id,
                revision=data["revision"],
                admin=request.user,
                confirm_reset=data["confirm_reset"]
            )
        except PotentialResetRequired as exc:
            return Response(exc.detail, status=status.HTTP_409_CONFLICT)
        return questionnaire_response(
            "Potential questionnaire published successfully.", assessment
        )