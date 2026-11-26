from django.shortcuts import get_object_or_404
from django.core.exceptions import ValidationError as DjangoValidationError
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from rest_framework import serializers
from evaluations.models import EmployeeQuestionnaire
from .descriptive_preference import get_descriptive_preference, set_descriptive_preference

class DescriptivePreferenceSerializer(serializers.Serializer):
    descriptive_scoring_enabled = serializers.BooleanField(required=True)
    confirm_invalidation = serializers.BooleanField(required=False, default=False)

class DescriptiveScoringPreferenceView(APIView):
    permission_classes = [IsAuthenticated]

    def _is_admin(self, user):
        return user.is_authenticated and user.is_active and str(getattr(user, "employee_number", "")) == "100607"

    def get(self, request, questionnaire_id):
        if not self._is_admin(request.user):
            return Response({"error": "Not authorized."}, status=403)
        questionnaire = get_object_or_404(EmployeeQuestionnaire, pk=questionnaire_id)
        return Response(get_descriptive_preference(questionnaire))

    def put(self, request, questionnaire_id):
        if not self._is_admin(request.user):
            return Response({"error": "Not authorized."}, status=403)
        serializer = DescriptivePreferenceSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        questionnaire = get_object_or_404(EmployeeQuestionnaire, pk=questionnaire_id)
        try:
            result = set_descriptive_preference(
                questionnaire, serializer.validated_data["descriptive_scoring_enabled"],
                request.user, serializer.validated_data["confirm_invalidation"]
            )
        except DjangoValidationError as exc:
            return Response({"error": exc.messages}, status=400)
        return Response({
            "message": "Descriptive scoring preference saved successfully.",
            **result
        })