from django.shortcuts import get_object_or_404
from django.core.exceptions import ValidationError as DjangoValidationError
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from rest_framework import serializers
from .models import EmployeeQuestionnaire, EmployeeQuestionnaireResponse
from .descriptive_scoring import save_descriptive_score, save_descriptive_scores


def _is_admin(user):
    return str(getattr(user, "employee_number", "")) == "100607"


def _validation_error(exc):
    return Response({"error": exc.messages}, status=400)


class DescriptiveScoreInputSerializer(serializers.Serializer):
    response_id = serializers.IntegerField(min_value=1)
    score = serializers.IntegerField(min_value=0)
    remarks = serializers.CharField(required=False, allow_blank=True, default="")


class BulkDescriptiveScoreSerializer(serializers.Serializer):
    scores = DescriptiveScoreInputSerializer(many=True, allow_empty=False)


class AdminDescriptiveResponsesView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, questionnaire_id):
        if not _is_admin(request.user):
            return Response({"error": "Not authorized."}, status=403)

        questionnaire = get_object_or_404(
            EmployeeQuestionnaire.objects.select_related("employee", "review_cycle"),
            pk=questionnaire_id
        )

        responses = EmployeeQuestionnaireResponse.objects.filter(
            submission__employee_questionnaire=questionnaire,
            submission__status="submitted",
            is_not_applicable=False
        ).select_related(
            "submission__submitted_by",
            "questionnaire_item__parameter__category",
            "admin_descriptive_score"
        ).order_by(
            "submission__evaluator_type",
            "submission__submitted_by_id",
            "questionnaire_item__order",
            "id"
        )

        results = []
        for response in responses:
            item = response.questionnaire_item
            question_type = item.question_type_snapshot or item.parameter.question_type

            if question_type != "descriptive" or not (response.descriptive_answer or "").strip():
                continue

            max_marks = item.max_marks_snapshot
            if max_marks is None:
                max_marks = item.parameter.max_marks

            score_record = getattr(response, "admin_descriptive_score", None)
            submitted_by = response.submission.submitted_by

            results.append({
                "response_id": response.id,
                "item_id": item.id,
                "question": item.question_text_snapshot or item.parameter.question_text,
                "category": item.parameter.category.name,
                "category_type": item.category_type_snapshot or item.parameter.category.category_type,
                "weightage": item.weightage,
                "max_marks": max_marks,
                "evaluator_type": response.submission.evaluator_type,
                "submitted_by_id": response.submission.submitted_by_id,
                "submitted_by": submitted_by.full_name if submitted_by else None,
                "descriptive_answer": response.descriptive_answer,
                "submitted_at": response.submission.submitted_at,
                "admin_score": score_record.score if score_record else None,
                "admin_remarks": score_record.remarks if score_record else "",
                "scored_by_id": score_record.scored_by_id if score_record else None,
                "scored_at": score_record.scored_at if score_record else None
            })

        return Response({
            "questionnaire_id": questionnaire.id,
            "employee_name": questionnaire.employee.full_name,
            "review_cycle": questionnaire.review_cycle.name,
            "questionnaire_status": questionnaire.status,
            "total_descriptive_responses": len(results),
            "scored_responses": sum(r["admin_score"] is not None for r in results),
            "responses": results
        })


class AdminSaveDescriptiveScoreView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        if not _is_admin(request.user):
            return Response({"error": "Not authorized."}, status=403)

        serializer = DescriptiveScoreInputSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        try:
            result = save_descriptive_score(
                serializer.validated_data["response_id"],
                serializer.validated_data["score"],
                request.user,
                serializer.validated_data["remarks"]
            )
        except EmployeeQuestionnaireResponse.DoesNotExist:
            return Response({"error": "Descriptive response not found."}, status=404)
        except DjangoValidationError as exc:
            return _validation_error(exc)

        return Response({
            "message": "Descriptive score saved successfully.",
            "result": result
        })


class AdminBulkDescriptiveScoreView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        if not _is_admin(request.user):
            return Response({"error": "Not authorized."}, status=403)

        serializer = BulkDescriptiveScoreSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        try:
            results = save_descriptive_scores(
                serializer.validated_data["scores"],
                request.user
            )
        except DjangoValidationError as exc:
            return _validation_error(exc)

        return Response({
            "message": "Descriptive scores saved successfully.",
            "updated_count": len(results),
            "results": results
        })