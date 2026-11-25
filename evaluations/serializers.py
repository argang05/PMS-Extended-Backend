from rest_framework import serializers
from .models import *


class QuestionnaireItemSerializer(serializers.ModelSerializer):
    question_text = serializers.CharField(source="parameter.question_text")
    category = serializers.CharField(source="parameter.category.name")
    category_type = serializers.CharField(source="parameter.category.category_type")

    class Meta:
        model = EmployeeQuestionnaireItem
        fields = ["id", "question_text", "category", "category_type", "weightage", "order"]


class QuestionnaireResponseSerializer(serializers.Serializer):
    item_id = serializers.IntegerField()
    score = serializers.IntegerField()


class PeerAssignmentSerializer(serializers.Serializer):
    peer_ids = serializers.ListField(
        child=serializers.IntegerField(min_value=1),
        min_length=1,
        max_length=3,
        allow_empty=False
    )

    def validate_peer_ids(self, value):
        if len(value) != len(set(value)):
            raise serializers.ValidationError("Duplicate peer reviewers are not allowed.")
        return value


class PeerResponseSerializer(serializers.Serializer):
    item_id = serializers.IntegerField(min_value=1)
    score = serializers.IntegerField(min_value=0, required=False, allow_null=True)
    descriptive_answer = serializers.CharField(
        required=False, allow_blank=True, allow_null=True
    )
    is_not_applicable = serializers.BooleanField(required=False, default=False)


class PeerReviewSubmissionSerializer(serializers.Serializer):
    questionnaire_id = serializers.IntegerField(min_value=1)
    responses = PeerResponseSerializer(many=True)
    feedback = serializers.CharField(required=False, allow_blank=True)
    scope_of_improvement = serializers.CharField(required=False, allow_blank=True)