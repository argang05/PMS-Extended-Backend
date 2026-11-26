from django.db.models import Q
from rest_framework.views import APIView
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.exceptions import ValidationError
from .serializers import PotentialEvaluationListSerializer, PotentialEvaluationSubmitSerializer
from .services import (
    manager_assessment_queryset, get_evaluation_detail,
    prepare_legacy_evaluation, submit_evaluation
)

class PotentialEvaluationAPIView(APIView):
    permission_classes = [IsAuthenticated]

class PotentialEvaluationListView(PotentialEvaluationAPIView):
    def get(self, request):
        queryset = manager_assessment_queryset(request.user)
        evaluator = request.query_params.get("evaluator_type", "")
        assessment_status = request.query_params.get("status", "")
        cycle_id = request.query_params.get("cycle_id", "")
        search = request.query_params.get("search", "").strip()
        submitted = request.query_params.get("submitted", "").lower()
        if evaluator and evaluator not in ("rm", "skip"):
            raise ValidationError({"evaluator_type": "Invalid evaluator type."})
        if evaluator == "rm":
            queryset = queryset.filter(reporting_manager_id=request.user.pk)
        elif evaluator == "skip":
            queryset = queryset.filter(skip_level_manager_id=request.user.pk)
        if assessment_status:
            queryset = queryset.filter(status=assessment_status)
        if cycle_id:
            if not cycle_id.isdigit():
                raise ValidationError({"cycle_id": "Invalid review cycle ID."})
            queryset = queryset.filter(review_cycle_id=int(cycle_id))
        if search:
            queryset = queryset.filter(
                Q(employee__first_name__icontains=search)
                | Q(employee__last_name__icontains=search)
                | Q(employee__employee_number__icontains=search)
                | Q(employee__department__icontains=search)
            )
        if submitted in ("true", "false"):
            submitted_value = submitted == "true"
            queryset = queryset.filter(
                Q(reporting_manager_id=request.user.pk, rm_submitted=submitted_value)
                | Q(skip_level_manager_id=request.user.pk, skip_submitted=submitted_value)
            )
        return Response(PotentialEvaluationListSerializer(
            queryset, many=True, context={"request": request}
        ).data)

class PotentialEvaluationDetailView(PotentialEvaluationAPIView):
    def get(self, request, assessment_id):
        return Response(get_evaluation_detail(assessment_id, request.user))

class PotentialLegacyEvaluationPrepareView(PotentialEvaluationAPIView):
    def post(self, request, assessment_id):
        evaluation = prepare_legacy_evaluation(assessment_id, request.user)
        return Response({
            "message": "Potential evaluation prepared successfully.",
            "evaluation": evaluation
        })

class PotentialEvaluationSubmitView(PotentialEvaluationAPIView):
    def post(self, request, assessment_id):
        serializer = PotentialEvaluationSubmitSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        evaluation = submit_evaluation(
            assessment_id=assessment_id, user=request.user,
            revision=data["revision"], responses=data["responses"]
        )
        return Response({
            "message": "Potential evaluation submitted successfully.",
            "evaluation": evaluation
        })