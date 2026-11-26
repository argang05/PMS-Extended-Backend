from rest_framework.views import APIView
from rest_framework.response import Response
from scoring.potential_question_bank.permissions import IsPotentialQuestionBankAdmin
from .serializers import (
    PotentialDescriptivePreferenceSerializer, PotentialDescriptiveBulkScoreSerializer,
    PotentialDescriptiveSingleScoreSerializer, PotentialDescriptiveClearScoreSerializer
)
from .services import (
    retrieve_preference, retrieve_workspace, set_preference,
    save_descriptive_scores, clear_descriptive_scores, get_descriptive_history
)

class PotentialDescriptiveAdminView(APIView):
    permission_classes = [IsPotentialQuestionBankAdmin]

class PotentialDescriptivePreferenceView(PotentialDescriptiveAdminView):
    def get(self, request, assessment_id):
        return Response(retrieve_preference(assessment_id))

    def put(self, request, assessment_id):
        serializer = PotentialDescriptivePreferenceSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        result = set_preference(
            assessment_id=assessment_id, enabled=data["is_enabled"],
            admin=request.user, confirm_invalidation=data["confirm_invalidation"]
        )
        return Response({
            "message": "Potential descriptive scoring preference saved successfully.",
            **result
        })

class PotentialDescriptiveWorkspaceView(PotentialDescriptiveAdminView):
    def get(self, request, assessment_id):
        return Response(retrieve_workspace(assessment_id))

class PotentialDescriptiveReadinessView(PotentialDescriptiveAdminView):
    def get(self, request, assessment_id):
        preference = retrieve_preference(assessment_id)
        return Response({
            "assessment_id": assessment_id,
            "revision": preference["revision"],
            "has_dependent_results": preference["has_dependent_results"],
            "dependent_results": preference["dependent_results"],
            **preference["readiness"]
        })

class PotentialDescriptiveSingleScoreView(PotentialDescriptiveAdminView):
    def post(self, request, assessment_id):
        serializer = PotentialDescriptiveSingleScoreSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        result = save_descriptive_scores(
            assessment_id=assessment_id, scores=[{
                "questionnaire_item_id": data["questionnaire_item_id"],
                "score": data["score"], "remarks": data["remarks"]
            }], revision=data["revision"], admin=request.user,
            confirm_invalidation=data["confirm_invalidation"]
        )
        return Response({
            "message": "Potential descriptive score saved successfully.",
            **result
        })

class PotentialDescriptiveBulkScoreView(PotentialDescriptiveAdminView):
    def post(self, request, assessment_id):
        serializer = PotentialDescriptiveBulkScoreSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        result = save_descriptive_scores(
            assessment_id=assessment_id, scores=data["scores"],
            revision=data["revision"], admin=request.user,
            confirm_invalidation=data["confirm_invalidation"]
        )
        return Response({
            "message": "Potential descriptive scores saved successfully.",
            **result
        })

class PotentialDescriptiveClearScoreView(PotentialDescriptiveAdminView):
    def post(self, request, assessment_id):
        serializer = PotentialDescriptiveClearScoreSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        result = clear_descriptive_scores(
            assessment_id=assessment_id, item_ids=data["questionnaire_item_ids"],
            revision=data["revision"], admin=request.user,
            confirm_invalidation=data["confirm_invalidation"]
        )
        return Response({
            "message": "Selected Potential descriptive scores cleared successfully.",
            **result
        })

class PotentialDescriptiveHistoryView(PotentialDescriptiveAdminView):
    def get(self, request, assessment_id):
        return Response({
            "assessment_id": assessment_id,
            "history": get_descriptive_history(assessment_id)
        })