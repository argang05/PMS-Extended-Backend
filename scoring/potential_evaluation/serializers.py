from math import isfinite
from rest_framework import serializers
from scoring.models import EmployeePotentialAssessment, EmployeePotentialResponse
from scoring.potential_question_bank.serializers import PotentialEmployeeSerializer

class PotentialEvaluationListSerializer(serializers.ModelSerializer):
    employee = PotentialEmployeeSerializer(read_only=True)
    review_cycle_name = serializers.CharField(source="review_cycle.name", read_only=True)
    review_cycle_id = serializers.IntegerField(read_only=True)
    employee_questionnaire_id = serializers.IntegerField(read_only=True)
    evaluator_type = serializers.SerializerMethodField()
    can_evaluate = serializers.SerializerMethodField()
    is_submitted = serializers.SerializerMethodField()
    question_count = serializers.IntegerField(read_only=True)

    class Meta:
        model = EmployeePotentialAssessment
        fields = [
            "id", "employee", "review_cycle_id", "review_cycle_name",
            "employee_questionnaire_id", "status", "is_published",
            "rm_submitted", "skip_submitted", "rm_submitted_at",
            "skip_submitted_at", "evaluator_type", "can_evaluate",
            "is_submitted", "question_count", "published_at", "created_at"
        ]

    def get_evaluator_type(self, obj):
        user = self.context["request"].user
        if obj.reporting_manager_id == user.pk:
            return "rm"
        if obj.skip_level_manager_id == user.pk:
            return "skip"
        return None

    def get_can_evaluate(self, obj):
        evaluator = self.get_evaluator_type(obj)
        if not obj.is_published:
            return False
        if evaluator == "rm":
            return obj.status == "pending_rm" and not obj.rm_submitted
        if evaluator == "skip":
            return obj.status == "pending_skip" and obj.rm_submitted and not obj.skip_submitted
        return False

    def get_is_submitted(self, obj):
        evaluator = self.get_evaluator_type(obj)
        if evaluator == "rm":
            return obj.rm_submitted
        if evaluator == "skip":
            return obj.skip_submitted
        return False

class PotentialEvaluationResponseSerializer(serializers.ModelSerializer):
    questionnaire_item_id = serializers.IntegerField(read_only=True)
    parameter_id = serializers.IntegerField(read_only=True)

    class Meta:
        model = EmployeePotentialResponse
        fields = [
            "id", "questionnaire_item_id", "parameter_id",
            "score", "descriptive_answer", "is_not_applicable",
            "evaluator_type", "submitted_at"
        ]

class PotentialEvaluationAnswerSerializer(serializers.Serializer):
    questionnaire_item_id = serializers.IntegerField(min_value=1)
    score = serializers.FloatField(required=False, allow_null=True)
    descriptive_answer = serializers.CharField(
        required=False, allow_null=True, allow_blank=True, trim_whitespace=True
    )

    def validate_score(self, value):
        if value is not None and not isfinite(value):
            raise serializers.ValidationError("Enter a valid numerical score.")
        return value

    def validate(self, attrs):
        score = attrs.get("score")
        answer = (attrs.get("descriptive_answer") or "").strip()
        if score is not None and answer:
            raise serializers.ValidationError("A response cannot contain both numerical and descriptive answers.")
        attrs["score"] = score
        attrs["descriptive_answer"] = answer or None
        return attrs

class PotentialEvaluationSubmitSerializer(serializers.Serializer):
    revision = serializers.DateTimeField()
    responses = PotentialEvaluationAnswerSerializer(many=True, allow_empty=False, max_length=1000)

    def validate_responses(self, value):
        ids = [row["questionnaire_item_id"] for row in value]
        if len(ids) != len(set(ids)):
            raise serializers.ValidationError("Duplicate question responses are not allowed.")
        return value