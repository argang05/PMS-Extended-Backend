from django.db import transaction
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from .views import is_scoring_admin
from .scoring_locks import lock_questionnaire
from .score_readiness import get_scoring_readiness
from .services_analytics_release import get_analytics_readiness
from .descriptive_preference import get_descriptive_preference

class ScoringReadinessView(APIView):
    permission_classes = [IsAuthenticated]

    @transaction.atomic
    def get(self, request, questionnaire_id):
        if not is_scoring_admin(request.user):
            return Response({"error": "Not authorized."}, status=403)
        questionnaire = lock_questionnaire(questionnaire_id)
        scoring = get_scoring_readiness(questionnaire)
        analytics = get_analytics_readiness(questionnaire, scoring=scoring)
        performance_preference = get_descriptive_preference(questionnaire)
        return Response({
            **scoring,
            "employee": {
                "id": questionnaire.employee_id,
                "employee_number": questionnaire.employee.employee_number,
                "full_name": questionnaire.employee.full_name,
                "department": questionnaire.employee.department,
                "designation": questionnaire.employee.designation
            },
            "review_cycle": {
                "id": questionnaire.review_cycle_id,
                "name": questionnaire.review_cycle.name
            },
            "questionnaire_status": questionnaire.status,
            "performance_preference": performance_preference,
            "analytics_release": analytics
        })