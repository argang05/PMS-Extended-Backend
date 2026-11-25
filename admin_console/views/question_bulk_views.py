from django.db import transaction
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from rest_framework.exceptions import ValidationError
from evaluations.models import QuestionParameter
from admin_console.serializers import QuestionParameterCreateUpdateSerializer
from .question_views import (
    is_admin_user, question_queryset, serialize_question,
    synchronize_questionnaires
)

BULK_EDITABLE_FIELDS = {
    "question_type", "category_id", "default_weightage",
    "max_marks", "is_active"
}

class AdminQuestionBulkUpdateView(APIView):
    permission_classes = [IsAuthenticated]

    @transaction.atomic
    def post(self, request):
        if not is_admin_user(request.user):
            return Response({"error": "Not authorized"}, status=403)

        question_ids = request.data.get("question_ids")
        updates = request.data.get("updates")

        if not isinstance(question_ids, list) or not question_ids:
            raise ValidationError({
                "question_ids": "Provide a non-empty list of question IDs."
            })
        if not isinstance(updates, dict) or not updates:
            raise ValidationError({
                "updates": "Provide at least one field to update."
            })

        invalid_fields = set(updates) - BULK_EDITABLE_FIELDS
        if invalid_fields:
            raise ValidationError({
                "updates": f"Unsupported bulk update fields: {', '.join(sorted(invalid_fields))}"
            })

        normalized_ids = []
        for question_id in question_ids:
            if isinstance(question_id, bool):
                raise ValidationError({"question_ids": "Question IDs must be positive integers."})
            try:
                parsed_id = int(question_id)
            except (TypeError, ValueError, OverflowError):
                raise ValidationError({"question_ids": "Question IDs must be positive integers."})
            if parsed_id <= 0 or str(question_id).strip() != str(parsed_id):
                raise ValidationError({"question_ids": "Question IDs must be positive integers."})
            if parsed_id not in normalized_ids:
                normalized_ids.append(parsed_id)

        questions = list(
            QuestionParameter.objects.select_for_update()
            .filter(id__in=normalized_ids).order_by("id")
        )
        found_ids = {question.id for question in questions}
        missing_ids = sorted(set(normalized_ids) - found_ids)
        if missing_ids:
            raise ValidationError({"question_ids": f"Questions not found: {missing_ids}"})

        validated_updates = []
        for question in questions:
            serializer = QuestionParameterCreateUpdateSerializer(
                question, data=updates, partial=True
            )
            try:
                serializer.is_valid(raise_exception=True)
            except ValidationError as exc:
                raise ValidationError({
                    "question_id": question.id,
                    "details": exc.detail
                })
            validated_updates.append((question, serializer))

        activation_changed_ids = []
        updated_ids = []
        for question, serializer in validated_updates:
            old_active = question.is_active
            serializer.save()
            updated_ids.append(question.id)
            if "is_active" in updates and old_active != question.is_active:
                activation_changed_ids.append(question.id)

        sync_result = (
            synchronize_questionnaires(activation_changed_ids)
            if activation_changed_ids else
            {"synchronized": 0, "skipped": 0, "created": 0, "removed": 0}
        )

        updated_questions = question_queryset().filter(
            id__in=updated_ids
        ).order_by("id")

        return Response({
            "message": f"{len(updated_ids)} questions updated successfully.",
            "updated_count": len(updated_ids),
            "questions": [serialize_question(question) for question in updated_questions],
            "questionnaire_sync": sync_result
        })