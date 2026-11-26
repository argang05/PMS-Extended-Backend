from math import isfinite
from rest_framework import serializers
from scoring.models import (
    PotentialParameter, EmployeePotentialAssessment,
    EmployeePotentialQuestionnaireItem, EmployeePotentialQuestionnaireCustomization
)
from scoring.potential_question_bank.serializers import PotentialEmployeeSerializer

class PotentialQuestionnaireCreateSerializer(serializers.Serializer):
    employee_id = serializers.IntegerField(min_value=1)
    review_cycle_id = serializers.IntegerField(min_value=1)

class PotentialQuestionnaireItemSerializer(serializers.ModelSerializer):
    parameter_id = serializers.IntegerField(read_only=True)
    question_text = serializers.SerializerMethodField()
    question_type = serializers.SerializerMethodField()
    max_marks = serializers.SerializerMethodField()
    category_name = serializers.SerializerMethodField()
    default_weightage = serializers.FloatField(source="parameter.weightage", read_only=True)
    is_active = serializers.BooleanField(source="parameter.is_active", read_only=True)
    target_mode = serializers.CharField(source="parameter.target_mode", read_only=True)

    class Meta:
        model = EmployeePotentialQuestionnaireItem
        fields = [
            "id", "parameter_id", "question_text", "question_type",
            "max_marks", "category_name", "weightage", "default_weightage",
            "order", "source", "is_active", "target_mode",
            "question_text_snapshot", "question_type_snapshot",
            "max_marks_snapshot", "category_name_snapshot"
        ]

    def get_question_text(self, obj):
        return obj.effective_question_text if obj.assessment.is_published else obj.parameter.question_text

    def get_question_type(self, obj):
        return obj.effective_question_type if obj.assessment.is_published else obj.parameter.question_type

    def get_max_marks(self, obj):
        return obj.effective_max_marks if obj.assessment.is_published else obj.parameter.max_marks

    def get_category_name(self, obj):
        if obj.assessment.is_published:
            return obj.category_name_snapshot
        return obj.parameter.category.name if obj.parameter.category_id else None

class PotentialQuestionnaireCustomizationSerializer(serializers.ModelSerializer):
    parameter_id = serializers.IntegerField(read_only=True)

    class Meta:
        model = EmployeePotentialQuestionnaireCustomization
        fields = ["parameter_id", "action", "created_at"]

class PotentialQuestionnaireListSerializer(serializers.ModelSerializer):
    employee = PotentialEmployeeSerializer(read_only=True)
    reporting_manager = PotentialEmployeeSerializer(read_only=True)
    skip_level_manager = PotentialEmployeeSerializer(read_only=True)
    review_cycle_name = serializers.CharField(source="review_cycle.name", read_only=True)
    review_cycle_id = serializers.IntegerField(read_only=True)
    employee_questionnaire_id = serializers.IntegerField(read_only=True)
    question_count = serializers.IntegerField(read_only=True)
    objective_count = serializers.IntegerField(read_only=True)
    descriptive_count = serializers.IntegerField(read_only=True)
    is_locked = serializers.SerializerMethodField()
    has_customization_draft = serializers.SerializerMethodField()
    has_responses = serializers.SerializerMethodField()
    activation_type = serializers.SerializerMethodField()

    class Meta:
        model = EmployeePotentialAssessment
        fields = [
            "id", "employee", "review_cycle_id", "review_cycle_name",
            "employee_questionnaire_id", "status", "is_published",
            "is_locked", "published_at", "reporting_manager",
            "skip_level_manager", "rm_submitted", "skip_submitted",
            "rm_submitted_at", "skip_submitted_at", "question_count",
            "objective_count", "descriptive_count", "created_at",
            "updated_at", "has_customization_draft", "has_responses",
            "activation_type"
        ]

    def get_is_locked(self, obj):
        return not obj.employee.is_active

    def get_has_customization_draft(self, obj):
        return obj.customization_draft is not None

    def get_has_responses(self, obj):
        return bool(getattr(obj, "has_responses", False))

    def get_activation_type(self, obj):
        if not obj.is_published:
            return "inactive"
        return "customized" if obj.published_by_id else "automatic"

class PotentialQuestionnaireDetailSerializer(PotentialQuestionnaireListSerializer):
    items = serializers.SerializerMethodField()
    customizations = serializers.SerializerMethodField()
    active_items = PotentialQuestionnaireItemSerializer(source="items", many=True, read_only=True)
    active_customizations = PotentialQuestionnaireCustomizationSerializer(source="customizations", many=True, read_only=True)
    total_weightage = serializers.SerializerMethodField()
    active_total_weightage = serializers.SerializerMethodField()
    revision = serializers.SerializerMethodField()
    reset_requirements = serializers.SerializerMethodField()

    class Meta(PotentialQuestionnaireListSerializer.Meta):
        fields = PotentialQuestionnaireListSerializer.Meta.fields + [
            "items", "customizations", "total_weightage", "revision",
            "active_items", "active_customizations", "active_total_weightage",
            "reset_requirements"
        ]

    def get_items(self, obj):
        if obj.customization_draft is None:
            return PotentialQuestionnaireItemSerializer(obj.items.all(), many=True).data
        from .services import get_automatic_questions
        rows = obj.customization_draft.get("items", [])
        parameters = {
            parameter.pk: parameter
            for parameter in PotentialParameter.objects.select_related("category").filter(
                pk__in=[row["parameter_id"] for row in rows]
            )
        }
        active_items = {item.parameter_id: item for item in obj.items.all()}
        automatic_ids = {question.pk for question in get_automatic_questions(obj)}
        result = []
        for order, row in enumerate(rows, start=1):
            parameter_id = row["parameter_id"]
            parameter = parameters.get(parameter_id)
            active_item = active_items.get(parameter_id)
            result.append({
                "id": active_item.pk if active_item else None,
                "parameter_id": parameter_id,
                "question_text": parameter.question_text if parameter else "Unavailable question",
                "question_type": parameter.question_type if parameter else "objective",
                "max_marks": parameter.max_marks if parameter else 10,
                "category_name": parameter.category.name if parameter and parameter.category_id else None,
                "weightage": row["weightage"],
                "default_weightage": parameter.weightage if parameter else row["weightage"],
                "order": order,
                "source": "automatic" if parameter_id in automatic_ids else "manual",
                "is_active": bool(parameter and parameter.is_active),
                "target_mode": parameter.target_mode if parameter else "manual",
                "question_text_snapshot": None,
                "question_type_snapshot": None,
                "max_marks_snapshot": None,
                "category_name_snapshot": None
            })
        return result

    def get_customizations(self, obj):
        if obj.customization_draft is None:
            return PotentialQuestionnaireCustomizationSerializer(obj.customizations.all(), many=True).data
        from .services import get_automatic_questions
        automatic_ids = {question.pk for question in get_automatic_questions(obj)}
        desired_ids = {row["parameter_id"] for row in obj.customization_draft.get("items", [])}
        excluded_ids = set(obj.customization_draft.get("excluded_parameter_ids", []))
        return [
            {"parameter_id": parameter_id, "action": "include", "created_at": None}
            for parameter_id in sorted(desired_ids - automatic_ids)
        ] + [
            {"parameter_id": parameter_id, "action": "exclude", "created_at": None}
            for parameter_id in sorted(excluded_ids)
        ]

    def get_total_weightage(self, obj):
        if obj.customization_draft is not None:
            return sum(row["weightage"] for row in obj.customization_draft.get("items", []))
        return sum(item.weightage for item in obj.items.all())

    def get_active_total_weightage(self, obj):
        return sum(item.weightage for item in obj.items.all())

    def get_revision(self, obj):
        return obj.updated_at.isoformat()

    def get_reset_requirements(self, obj):
        from .services import get_reset_requirements
        return get_reset_requirements(obj)

class PotentialDraftItemSerializer(serializers.Serializer):
    parameter_id = serializers.IntegerField(min_value=1)
    weightage = serializers.FloatField()

    def validate_weightage(self, value):
        if not isfinite(value) or value <= 0:
            raise serializers.ValidationError("Weightage must be greater than zero.")
        return value

class PotentialDraftSaveSerializer(serializers.Serializer):
    revision = serializers.DateTimeField()
    items = PotentialDraftItemSerializer(many=True, allow_empty=True, max_length=1000)
    excluded_parameter_ids = serializers.ListField(
        child=serializers.IntegerField(min_value=1),
        required=False, default=list, max_length=1000
    )

    def validate(self, attrs):
        item_ids = [item["parameter_id"] for item in attrs["items"]]
        excluded_ids = set(attrs["excluded_parameter_ids"])
        if len(item_ids) != len(set(item_ids)):
            raise serializers.ValidationError({"items": "Duplicate questions are not allowed."})
        if set(item_ids) & excluded_ids:
            raise serializers.ValidationError({
                "excluded_parameter_ids": "A question cannot be included and excluded simultaneously."
            })
        attrs["excluded_parameter_ids"] = sorted(excluded_ids)
        return attrs

class PotentialRevisionSerializer(serializers.Serializer):
    revision = serializers.DateTimeField()

class PotentialQuestionnairePublishSerializer(PotentialRevisionSerializer):
    confirm_reset = serializers.BooleanField(default=False)

class PotentialQuestionnaireCopySerializer(PotentialRevisionSerializer):
    source_assessment_id = serializers.IntegerField(min_value=1)
    copy_weightages = serializers.BooleanField(default=True)