from math import isfinite
from django.contrib.auth import get_user_model
from rest_framework import serializers
from scoring.models import PotentialQuestionCategory, PotentialParameter

Employee = get_user_model()


class PotentialCategorySerializer(serializers.ModelSerializer):
    question_count = serializers.IntegerField(read_only=True)

    class Meta:
        model = PotentialQuestionCategory
        fields = ["id", "name", "question_count"]

    def validate_name(self, value):
        value = value.strip()
        if not value:
            raise serializers.ValidationError("Category name is required.")
        queryset = PotentialQuestionCategory.objects.filter(name__iexact=value)
        if self.instance:
            queryset = queryset.exclude(pk=self.instance.pk)
        if queryset.exists():
            raise serializers.ValidationError("A category with this name already exists.")
        return value


class PotentialEmployeeSerializer(serializers.ModelSerializer):
    full_name = serializers.SerializerMethodField()

    class Meta:
        model = Employee
        fields = ["id", "employee_number", "first_name", "last_name", "full_name", "department", "designation"]

    def get_full_name(self, obj):
        return f"{obj.first_name or ''} {obj.last_name or ''}".strip()


class PotentialQuestionSerializer(serializers.ModelSerializer):
    category_name = serializers.CharField(source="category.name", read_only=True, default=None)
    departments = serializers.SerializerMethodField()
    employee_ids = serializers.SerializerMethodField()
    excluded_employee_ids = serializers.SerializerMethodField()
    assigned_employees = serializers.SerializerMethodField()
    excluded_employees = serializers.SerializerMethodField()
    default_weightage = serializers.FloatField(source="weightage", read_only=True)
    is_configured = serializers.SerializerMethodField()
    is_used = serializers.SerializerMethodField()
    is_published_used = serializers.SerializerMethodField()

    class Meta:
        model = PotentialParameter
        fields = [
            "id", "category", "category_name", "question_text", "question_type",
            "max_marks", "weightage", "default_weightage", "target_mode",
            "is_active", "departments", "employee_ids", "excluded_employee_ids",
            "assigned_employees", "excluded_employees", "is_configured",
            "is_used", "is_published_used", "created_at", "updated_at"
        ]

    def get_departments(self, obj):
        return sorted({row.department for row in obj.department_assignments.all()})

    def get_employee_ids(self, obj):
        return sorted({row.employee_id for row in obj.employee_assignments.all()})

    def get_excluded_employee_ids(self, obj):
        return sorted({row.employee_id for row in obj.employee_exclusions.all()})

    def get_assigned_employees(self, obj):
        return PotentialEmployeeSerializer([row.employee for row in obj.employee_assignments.all()], many=True).data

    def get_excluded_employees(self, obj):
        return PotentialEmployeeSerializer([row.employee for row in obj.employee_exclusions.all()], many=True).data

    def get_is_configured(self, obj):
        return obj.target_mode in ["all", "departments", "employees", "combined"]

    def get_is_used(self, obj):
        if hasattr(obj, "used_flag"):
            return obj.used_flag
        return obj.questionnaire_items.exists()

    def get_is_published_used(self, obj):
        if hasattr(obj, "published_used_flag"):
            return obj.published_used_flag
        return obj.questionnaire_items.filter(assessment__is_published=True).exists()


class PotentialTargetingSerializer(serializers.Serializer):
    target_mode = serializers.ChoiceField(choices=["manual", "all", "departments", "employees", "combined"])
    departments = serializers.ListField(child=serializers.CharField(max_length=150), required=False, default=list)
    employee_ids = serializers.ListField(child=serializers.IntegerField(min_value=1), required=False, default=list)
    excluded_employee_ids = serializers.ListField(child=serializers.IntegerField(min_value=1), required=False, default=list)

    def validate(self, attrs):
        mode = attrs["target_mode"]
        departments = sorted({value.strip() for value in attrs.get("departments", []) if value.strip()})
        employee_ids = sorted(set(attrs.get("employee_ids", [])))
        excluded_ids = sorted(set(attrs.get("excluded_employee_ids", [])))
        if mode == "manual":
            departments, employee_ids, excluded_ids = [], [], []
        elif mode == "all":
            departments, employee_ids = [], []
        elif mode == "departments":
            if not departments:
                raise serializers.ValidationError({"departments": "Select at least one department."})
            employee_ids = []
        elif mode == "employees":
            if not employee_ids:
                raise serializers.ValidationError({"employee_ids": "Select at least one employee."})
            departments = []
        elif mode == "combined":
            if not departments or not employee_ids:
                raise serializers.ValidationError("Combined targeting requires departments and employees.")
        if departments:
            existing = set(Employee.objects.filter(is_active=True, department__in=departments).values_list("department", flat=True))
            missing = sorted(set(departments) - existing)
            if missing:
                raise serializers.ValidationError({"departments": f"Invalid or inactive departments: {', '.join(missing)}"})
        all_ids = set(employee_ids) | set(excluded_ids)
        if all_ids:
            existing_ids = set(Employee.objects.filter(pk__in=all_ids, is_active=True).values_list("pk", flat=True))
            missing_ids = sorted(all_ids - existing_ids)
            if missing_ids:
                raise serializers.ValidationError({"employee_ids": f"Invalid or inactive employee IDs: {missing_ids}"})
        attrs["departments"] = departments
        attrs["employee_ids"] = employee_ids
        attrs["excluded_employee_ids"] = excluded_ids
        return attrs


class PotentialQuestionWriteSerializer(serializers.Serializer):
    category = serializers.PrimaryKeyRelatedField(queryset=PotentialQuestionCategory.objects.all(), required=False, allow_null=True)
    question_text = serializers.CharField(trim_whitespace=True)
    question_type = serializers.ChoiceField(choices=PotentialParameter.QUESTION_TYPES, default="objective")
    max_marks = serializers.IntegerField(min_value=1, default=10)
    weightage = serializers.FloatField(required=False)
    default_weightage = serializers.FloatField(required=False)
    is_active = serializers.BooleanField(default=True)
    target_mode = serializers.ChoiceField(choices=PotentialParameter.TARGET_MODES, required=False)
    departments = serializers.ListField(child=serializers.CharField(max_length=150), required=False)
    employee_ids = serializers.ListField(child=serializers.IntegerField(min_value=1), required=False)
    excluded_employee_ids = serializers.ListField(child=serializers.IntegerField(min_value=1), required=False)

    def validate(self, attrs):
        instance = self.instance
        creating = instance is None
        if "weightage" in attrs and "default_weightage" in attrs:
            if attrs["weightage"] != attrs["default_weightage"]:
                raise serializers.ValidationError({"weightage": "Conflicting weightage values."})
        if "default_weightage" in attrs:
            attrs["weightage"] = attrs.pop("default_weightage")
        weightage = attrs.get("weightage", instance.weightage if instance else 1)
        if not isfinite(weightage) or weightage <= 0:
            raise serializers.ValidationError({"weightage": "Weightage must be greater than zero."})
        question_type = attrs.get("question_type", instance.question_type if instance else "objective")
        max_marks = attrs.get("max_marks", instance.max_marks if instance else 10)
        structural_change = creating or (
            instance and (
                question_type != instance.question_type or
                max_marks != instance.max_marks
            )
        )
        if question_type == "descriptive" and structural_change:
            if "max_marks" in attrs and max_marks != 10:
                raise serializers.ValidationError({"max_marks": "Descriptive questions must use a maximum of 10 marks."})
            attrs["max_marks"] = 10
        mode = attrs.get("target_mode", instance.target_mode if instance else "manual")
        if mode == "legacy" and (creating or instance.target_mode != "legacy"):
            raise serializers.ValidationError({"target_mode": "Legacy mode is reserved for migrated questions."})
        targeting_fields = {"target_mode", "departments", "employee_ids", "excluded_employee_ids"}
        if mode == "legacy" and any(field in attrs for field in targeting_fields - {"target_mode"}):
            raise serializers.ValidationError({"target_mode": "Choose a new targeting mode before assigning targets."})
        if mode != "legacy" and (creating or targeting_fields.intersection(attrs)):
            existing_departments = []
            existing_employees = []
            existing_exclusions = []
            if instance:
                existing_departments = list(instance.department_assignments.values_list("department", flat=True))
                existing_employees = list(instance.employee_assignments.values_list("employee_id", flat=True))
                existing_exclusions = list(instance.employee_exclusions.values_list("employee_id", flat=True))
            targeting = PotentialTargetingSerializer(data={
                "target_mode": mode,
                "departments": attrs.get("departments", existing_departments),
                "employee_ids": attrs.get("employee_ids", existing_employees),
                "excluded_employee_ids": attrs.get("excluded_employee_ids", existing_exclusions)
            })
            targeting.is_valid(raise_exception=True)
            attrs.update(targeting.validated_data)
        if creating:
            attrs.setdefault("weightage", weightage)
            attrs.setdefault("target_mode", "manual")
        return attrs


class PotentialBulkCreateSerializer(serializers.Serializer):
    questions = PotentialQuestionWriteSerializer(many=True, allow_empty=False, max_length=500)


class PotentialBulkConfigureSerializer(serializers.Serializer):
    question_ids = serializers.ListField(child=serializers.IntegerField(min_value=1), allow_empty=False, max_length=500)
    configuration = serializers.DictField()

    def validate_question_ids(self, value):
        return sorted(set(value))

    def validate_configuration(self, value):
        allowed = {
            "category", "question_type", "max_marks", "weightage",
            "default_weightage", "is_active", "target_mode",
            "departments", "employee_ids", "excluded_employee_ids"
        }
        if not value:
            raise serializers.ValidationError("Configuration cannot be empty.")
        unknown = set(value) - allowed
        if unknown:
            raise serializers.ValidationError(f"Unsupported configuration fields: {sorted(unknown)}")
        return value


class PotentialBulkDeleteSerializer(serializers.Serializer):
    question_ids = serializers.ListField(child=serializers.IntegerField(min_value=1), allow_empty=False, max_length=500)

    def validate_question_ids(self, value):
        return sorted(set(value))