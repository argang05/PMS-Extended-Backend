from django.db import transaction
from django.shortcuts import get_object_or_404
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from rest_framework.exceptions import ValidationError
from .models import EmployeeAppraisalRecommendation
from .scoring_locks import lock_questionnaire
from .service_appraisal_engine import (
    generate_appraisal_recommendation, get_appraisal_recommendation,
    apply_hr_override, get_appraisal_readiness
)

def is_scoring_admin(user):
    return user.is_authenticated and user.is_active and str(getattr(user, "employee_number", "")) == "100607"

def serialize_recommendation(questionnaire, recommendation):
    return {
        "questionnaire_id": questionnaire.pk,
        "employee_name": questionnaire.employee.full_name,
        "employee_number": questionnaire.employee.employee_number,
        "review_cycle": questionnaire.review_cycle.name if questionnaire.review_cycle else None,
        "box_label": recommendation.box_label,
        "ppi_score_used": recommendation.ppi_score_used,
        "potential_score_used": recommendation.potential_score_used,
        "min_appraisal_percent": recommendation.min_appraisal_percent,
        "max_appraisal_percent": recommendation.max_appraisal_percent,
        "suggested_appraisal_percent": recommendation.suggested_appraisal_percent,
        "hr_override_percent": recommendation.hr_override_percent,
        "override_reason": recommendation.override_reason,
        "final_effective_appraisal_percent": recommendation.final_effective_appraisal_percent,
        "overridden_by": recommendation.overridden_by.full_name if recommendation.overridden_by else None,
        "overridden_at": recommendation.overridden_at,
        "calculated_at": recommendation.calculated_at
    }

class GenerateAppraisalRecommendationView(APIView):
    permission_classes = [IsAuthenticated]

    @transaction.atomic
    def post(self, request, questionnaire_id):
        if not is_scoring_admin(request.user):
            return Response({"error": "You are not authorized to generate appraisal recommendation."}, status=403)
        questionnaire = lock_questionnaire(questionnaire_id)
        recommendation = generate_appraisal_recommendation(questionnaire, request.user)
        return Response({
            "message": "Appraisal recommendation generated successfully.",
            **serialize_recommendation(questionnaire, recommendation)
        })

class AppraisalRecommendationResultView(APIView):
    permission_classes = [IsAuthenticated]

    @transaction.atomic
    def get(self, request, questionnaire_id):
        if not is_scoring_admin(request.user):
            return Response({"error": "You are not authorized to view appraisal recommendation."}, status=403)
        questionnaire = lock_questionnaire(questionnaire_id)
        recommendation = get_appraisal_recommendation(questionnaire)
        if recommendation is None:
            return Response({"error": "Appraisal recommendation has not been generated yet."}, status=404)
        readiness = get_appraisal_readiness(questionnaire, recommendation)
        return Response({
            **serialize_recommendation(questionnaire, recommendation),
            "is_current": readiness["is_ready"],
            "missing_requirements": readiness["missing_requirements"]
        })

class AppraisalRecommendationOverrideView(APIView):
    permission_classes = [IsAuthenticated]

    @transaction.atomic
    def put(self, request, questionnaire_id):
        if not is_scoring_admin(request.user):
            return Response({"error": "You are not authorized to override appraisal recommendation."}, status=403)
        questionnaire = lock_questionnaire(questionnaire_id)
        recommendation = get_object_or_404(
            EmployeeAppraisalRecommendation, employee_questionnaire_id=questionnaire.pk
        )
        override_percent = request.data.get("hr_override_percent")
        override_reason = request.data.get("override_reason")
        if override_percent is None or override_percent == "":
            return Response({"error": "hr_override_percent is required."}, status=400)
        if not isinstance(override_reason, str) or not override_reason.strip():
            return Response({"error": "override_reason is required."}, status=400)
        recommendation = apply_hr_override(
            recommendation=recommendation, override_percent=override_percent,
            reason=override_reason, hr_employee=request.user
        )
        return Response({
            "message": "Appraisal override applied successfully.",
            **serialize_recommendation(questionnaire, recommendation)
        })