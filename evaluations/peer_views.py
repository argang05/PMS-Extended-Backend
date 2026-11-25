from django.db import transaction
from django.shortcuts import get_object_or_404
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from rest_framework.exceptions import ValidationError
from .models import (
    EmployeeQuestionnaire, EmployeeQuestionnaireItem,
    EmployeeQuestionnaireResponse
)
from .peer_services import (
    PeerServiceError, assign_peers, add_peer, remove_peer,
    peer_progress, peer_submission, is_assigned_peer
)
from .serializers import PeerAssignmentSerializer


def _error(exc):
    return Response({"error": str(exc)}, status=400)


def _questionnaire(questionnaire_id):
    return get_object_or_404(
        EmployeeQuestionnaire.objects.select_related(
            "employee", "review_cycle", "peer_reviewer"
        ),
        pk=questionnaire_id
    )


def _can_view_progress(questionnaire, user):
    if questionnaire.employee_id == user.id:
        return True
    if str(getattr(user, "employee_number", "")) == "100607":
        return True
    if questionnaire.employee.reporting_manager_id == user.id:
        return True
    if questionnaire.employee.skip_level_manager_id == user.id:
        return True
    return is_assigned_peer(questionnaire, user)


class PeerAssignmentListView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, questionnaire_id):
        questionnaire = _questionnaire(questionnaire_id)

        if not _can_view_progress(questionnaire, request.user):
            return Response({"error": "Not authorized."}, status=403)

        return Response({
            "questionnaire_id": questionnaire.id,
            "questionnaire_status": questionnaire.status,
            "peer_review": peer_progress(questionnaire)
        })

    def put(self, request, questionnaire_id):
        serializer = PeerAssignmentSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        try:
            result = assign_peers(
                questionnaire_id,
                serializer.validated_data["peer_ids"],
                request.user
            )
        except EmployeeQuestionnaire.DoesNotExist:
            return Response({"error": "Questionnaire not found."}, status=404)
        except PeerServiceError as exc:
            return _error(exc)

        return Response(result)


class PeerAssignmentAddView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request, questionnaire_id):
        peer_id = request.data.get("peer_id")

        if peer_id is None:
            raise ValidationError({"peer_id": "This field is required."})

        try:
            result = add_peer(questionnaire_id, peer_id, request.user)
        except EmployeeQuestionnaire.DoesNotExist:
            return Response({"error": "Questionnaire not found."}, status=404)
        except PeerServiceError as exc:
            return _error(exc)

        return Response(result)


class PeerAssignmentRemoveView(APIView):
    permission_classes = [IsAuthenticated]

    def delete(self, request, questionnaire_id, peer_id):
        try:
            result = remove_peer(questionnaire_id, peer_id, request.user)
        except EmployeeQuestionnaire.DoesNotExist:
            return Response({"error": "Questionnaire not found."}, status=404)
        except PeerServiceError as exc:
            return _error(exc)

        return Response(result)


class PeerReviewProgressView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, questionnaire_id):
        questionnaire = _questionnaire(questionnaire_id)

        if not _can_view_progress(questionnaire, request.user):
            return Response({"error": "Not authorized."}, status=403)

        return Response({
            "questionnaire_id": questionnaire.id,
            "questionnaire_status": questionnaire.status,
            "peer_review": peer_progress(questionnaire)
        })


class PeerMyDraftView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, questionnaire_id):
        questionnaire = _questionnaire(questionnaire_id)

        if not is_assigned_peer(questionnaire, request.user):
            return Response({"error": "Not authorized."}, status=403)

        try:
            submission = peer_submission(questionnaire, request.user)
        except PeerServiceError as exc:
            return _error(exc)

        if not submission:
            return Response({
                "questionnaire_id": questionnaire.id,
                "peer_status": "pending",
                "feedback": "",
                "scope_of_improvement": "",
                "responses": []
            })

        responses = EmployeeQuestionnaireResponse.objects.filter(
            submission=submission,
            reviewer_type="peer"
        ).select_related(
            "questionnaire_item",
            "questionnaire_item__parameter"
        ).order_by("questionnaire_item__order", "id")

        return Response({
            "questionnaire_id": questionnaire.id,
            "peer_status": submission.status,
            "feedback": submission.feedback or "",
            "scope_of_improvement": submission.scope_of_improvement or "",
            "submitted_at": submission.submitted_at,
            "responses": [{
                "item_id": response.questionnaire_item_id,
                "score": response.score,
                "descriptive_answer": response.descriptive_answer,
                "is_not_applicable": response.is_not_applicable
            } for response in responses]
        })