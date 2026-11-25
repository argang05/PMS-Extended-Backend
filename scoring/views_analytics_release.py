from django.shortcuts import get_object_or_404
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from evaluations.models import EmployeeQuestionnaire
from .models import EmployeeAnalyticsRelease
from .services_analytics_release import (
    get_or_create_analytics_release,
    get_analytics_readiness,
    release_analytics_to_employee,
    unrelease_analytics_to_employee,
    build_employee_analytics_report
)


def is_scoring_admin(user):
    return str(getattr(user, "employee_number", "")) == "100607"


class AnalyticsReleaseStatusView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, questionnaire_id):
        if not is_scoring_admin(request.user):
            return Response({"error": "Not authorized."}, status=403)

        questionnaire = get_object_or_404(
            EmployeeQuestionnaire.objects.select_related(
                "employee", "employee__reporting_manager",
                "review_cycle", "peer_reviewer"
            ),
            id=questionnaire_id
        )

        release = get_or_create_analytics_release(questionnaire)
        readiness = get_analytics_readiness(questionnaire)

        return Response({
            "questionnaire_id": questionnaire.id,
            "employee_name": questionnaire.employee.full_name,
            "employee_number": questionnaire.employee.employee_number,
            "review_cycle": questionnaire.review_cycle.name,
            "is_released_to_employee": release.is_released_to_employee,
            "released_at": release.released_at,
            "released_by": (
                getattr(release.released_by, "full_name", None)
                if release.released_by else None
            ),
            **readiness
        })


class AnalyticsReleaseToggleView(APIView):
    permission_classes = [IsAuthenticated]

    def put(self, request, questionnaire_id):
        if not is_scoring_admin(request.user):
            return Response({"error": "Not authorized."}, status=403)

        questionnaire = get_object_or_404(
            EmployeeQuestionnaire.objects.select_related(
                "employee", "employee__reporting_manager",
                "review_cycle", "peer_reviewer"
            ),
            id=questionnaire_id
        )

        release_requested = request.data.get("is_released_to_employee")

        if not isinstance(release_requested, bool):
            return Response({
                "error": "is_released_to_employee must be a boolean."
            }, status=400)

        result = (
            release_analytics_to_employee(questionnaire, request.user)
            if release_requested
            else unrelease_analytics_to_employee(questionnaire, request.user)
        )

        if not result["success"]:
            return Response({
                "error": result["message"],
                "missing_requirements": result.get("missing_requirements", [])
            }, status=400)

        release = get_or_create_analytics_release(questionnaire)

        return Response({
            "message": result["message"],
            "questionnaire_id": questionnaire.id,
            "is_released_to_employee": release.is_released_to_employee,
            "released_at": release.released_at,
            "unreleased_at": release.unreleased_at
        })


class MyAnalyticsReportView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, questionnaire_id):
        questionnaire = get_object_or_404(
            EmployeeQuestionnaire.objects.select_related(
                "employee", "employee__reporting_manager",
                "review_cycle", "peer_reviewer"
            ),
            id=questionnaire_id,
            employee=request.user
        )

        release = EmployeeAnalyticsRelease.objects.filter(
            employee_questionnaire=questionnaire
        ).first()

        if not release or not release.is_released_to_employee:
            return Response({
                "error": "Analytics report has not been released yet."
            }, status=403)

        readiness = get_analytics_readiness(questionnaire)

        if not readiness["is_ready"]:
            return Response({
                "error": "Analytics report is temporarily unavailable because its underlying data requires revalidation.",
                "missing_requirements": readiness["missing_requirements"]
            }, status=409)

        report = build_employee_analytics_report(questionnaire)

        role_names = {
            "self": "Self",
            "rm": "Reporting Manager",
            "skip": "Skip-Level Manager",
            "peer": "Peer Reviewer"
        }

        for section in report["feedback_sections"]:
            section["role"] = role_names.get(
                section["role"], section["role"]
            )

        return Response(report)