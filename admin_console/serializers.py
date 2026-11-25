from rest_framework import serializers
from django.db import transaction
from evaluations.models import QuestionCategory, QuestionParameter, ReviewCycle
from scoring.models import (
    PotentialParameter, ScoreConfiguration, PotentialScoreConfiguration,
    AppraisalRecommendationConfig
)

class QuestionCategorySerializer(serializers.ModelSerializer):
    class Meta:
        model = QuestionCategory
        fields = ["id", "name", "category_type"]

class QuestionParameterListSerializer(serializers.ModelSerializer):
    category = QuestionCategorySerializer(read_only=True)
    category_id = serializers.PrimaryKeyRelatedField(
        queryset=QuestionCategory.objects.all(), source="category",
        write_only=True, required=False, allow_null=True
    )

    class Meta:
        model = QuestionParameter
        fields = [
            "id", "question_text", "category", "category_id", "department",
            "default_weightage", "question_type", "max_marks",
            "target_mode", "is_active"
        ]

class QuestionParameterCreateUpdateSerializer(serializers.ModelSerializer):
    category_id = serializers.PrimaryKeyRelatedField(
        queryset=QuestionCategory.objects.all(), source="category",
        required=False, allow_null=True
    )

    class Meta:
        model = QuestionParameter
        fields = [
            "question_text", "category_id", "department", "default_weightage",
            "question_type", "max_marks", "target_mode", "is_active"
        ]
        extra_kwargs = {
            "question_type": {"required": False},
            "max_marks": {"required": False},
            "target_mode": {"required": False},
            "department": {"required": False},
            "default_weightage": {"required": False},
            "is_active": {"required": False},
        }

    def validate_question_text(self, value):
        value = value.strip()
        if not value:
            raise serializers.ValidationError("Question text cannot be empty.")
        return value

    def validate_max_marks(self, value):
        if value < 1:
            raise serializers.ValidationError("Maximum marks must be greater than zero.")
        return value

    def validate_default_weightage(self, value):
        if value <= 0:
            raise serializers.ValidationError("Question weightage must be greater than zero.")
        return value

    def validate(self, attrs):
        question_type = attrs.get(
            "question_type", getattr(self.instance, "question_type", "objective")
        )
        max_marks = attrs.get(
            "max_marks", getattr(self.instance, "max_marks", 10)
        )
        target_mode = attrs.get(
            "target_mode", getattr(self.instance, "target_mode", "manual")
        )
        category = attrs.get(
            "category", getattr(self.instance, "category", None)
        )

        if question_type not in ["objective", "descriptive"]:
            raise serializers.ValidationError({"question_type": "Invalid question type."})
        if max_marks < 1:
            raise serializers.ValidationError({"max_marks": "Maximum marks must be greater than zero."})

        valid_modes = [
            choice[0] for choice in QuestionParameter._meta.get_field("target_mode").choices
        ]
        if target_mode not in valid_modes:
            raise serializers.ValidationError({"target_mode": "Invalid targeting mode."})

        if category is None and target_mode != "manual":
            raise serializers.ValidationError({
                "category_id": "Assign a category before enabling automatic employee targeting."
            })

        return attrs

    @transaction.atomic
    def create(self, validated_data):
        return super().create(validated_data)

    @transaction.atomic
    def update(self, instance, validated_data):
        return super().update(instance, validated_data)

class PotentialParameterListSerializer(serializers.ModelSerializer):
    class Meta:
        model = PotentialParameter
        fields = ["id", "question_text", "weightage", "is_active", "created_at"]

class PotentialParameterCreateUpdateSerializer(serializers.ModelSerializer):
    class Meta:
        model = PotentialParameter
        fields = ["question_text", "weightage", "is_active"]

class ScoreConfigurationSerializer(serializers.ModelSerializer):
    class Meta:
        model = ScoreConfiguration
        fields = [
            "id", "name", "behavioural_weight", "performance_weight",
            "self_weight", "rm_weight", "skip_weight", "peer_weight",
            "descriptive_scoring_enabled", "objective_component_weight",
            "descriptive_component_weight", "is_active", "created_at"
        ]

    def validate(self, attrs):
        objective_weight = attrs.get(
            "objective_component_weight",
            getattr(self.instance, "objective_component_weight", 0.8)
        )
        descriptive_weight = attrs.get(
            "descriptive_component_weight",
            getattr(self.instance, "descriptive_component_weight", 0.2)
        )
        if abs(float(objective_weight) + float(descriptive_weight) - 1.0) > 0.000001:
            raise serializers.ValidationError({
                "objective_component_weight": "Objective and descriptive weights must sum to 1."
            })
        return attrs

class PotentialScoreConfigurationSerializer(serializers.ModelSerializer):
    class Meta:
        model = PotentialScoreConfiguration
        fields = [
            "id", "name", "rm_weight", "skip_weight",
            "is_active", "created_at"
        ]

class AppraisalRecommendationConfigSerializer(serializers.ModelSerializer):
    class Meta:
        model = AppraisalRecommendationConfig
        fields = [
            "id", "box_label", "min_percent", "max_percent",
            "is_active", "created_at", "updated_at"
        ]

class ReviewCycleSerializer(serializers.ModelSerializer):
    class Meta:
        model = ReviewCycle
        fields = ["id", "name", "start_date", "end_date", "is_active"]