from rest_framework import serializers

class PotentialDescriptivePreferenceSerializer(serializers.Serializer):
    is_enabled = serializers.BooleanField()
    confirm_invalidation = serializers.BooleanField(required=False, default=False)

class PotentialDescriptiveQuestionScoreSerializer(serializers.Serializer):
    questionnaire_item_id = serializers.IntegerField(min_value=1)
    score = serializers.IntegerField(min_value=1, max_value=10)
    remarks = serializers.CharField(required=False, allow_blank=True, default="", max_length=10000)

class PotentialDescriptiveBulkScoreSerializer(serializers.Serializer):
    scores = PotentialDescriptiveQuestionScoreSerializer(many=True, allow_empty=False, max_length=1000)
    confirm_invalidation = serializers.BooleanField(required=False, default=False)
    revision = serializers.DateTimeField()

    def validate_scores(self, value):
        item_ids = [row["questionnaire_item_id"] for row in value]
        if len(item_ids) != len(set(item_ids)):
            raise serializers.ValidationError("Duplicate descriptive question scores are not allowed.")
        return value

class PotentialDescriptiveSingleScoreSerializer(PotentialDescriptiveQuestionScoreSerializer):
    confirm_invalidation = serializers.BooleanField(required=False, default=False)
    revision = serializers.DateTimeField()

class PotentialDescriptiveClearScoreSerializer(serializers.Serializer):
    questionnaire_item_ids = serializers.ListField(
        child=serializers.IntegerField(min_value=1), allow_empty=False, max_length=1000
    )
    confirm_invalidation = serializers.BooleanField(required=False, default=False)
    revision = serializers.DateTimeField()

    def validate_questionnaire_item_ids(self, value):
        return sorted(set(value))