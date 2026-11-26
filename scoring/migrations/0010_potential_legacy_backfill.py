from collections import Counter
from math import isfinite
from django.db import migrations


def backfill_potential_data(apps, schema_editor):
    PotentialParameter = apps.get_model("scoring", "PotentialParameter")
    PotentialAssessment = apps.get_model("scoring", "EmployeePotentialAssessment")
    PotentialResponse = apps.get_model("scoring", "EmployeePotentialResponse")
    PotentialItem = apps.get_model("scoring", "EmployeePotentialQuestionnaireItem")
    PotentialDescriptiveAssessment = apps.get_model("scoring", "EmployeePotentialDescriptiveAssessment")

    db = schema_editor.connection.alias

    parameters = PotentialParameter.objects.using(db)
    assessments = PotentialAssessment.objects.using(db)
    responses = PotentialResponse.objects.using(db)
    items = PotentialItem.objects.using(db)
    descriptive_assessments = PotentialDescriptiveAssessment.objects.using(db)

    legacy_question_count = parameters.filter(target_mode="manual").update(target_mode="legacy")

    assessment_count = 0
    item_count = 0
    linked_response_count = 0
    duplicate_response_count = 0
    preference_count = 0
    invalid_weight_count = 0
    empty_assessment_count = 0

    for assessment in assessments.select_related("employee").iterator(chunk_size=500):
        employee = assessment.employee

        rm_id = employee.reporting_manager_id
        skip_id = employee.skip_level_manager_id

        # Prevent employees from evaluating their own potential.
        if rm_id == employee.pk:
            rm_id = None
        if skip_id == employee.pk:
            skip_id = None

        # Preserve existing legacy assessment accessibility.
        # Historical publication timestamps and publishers are unknown.
        assessments.filter(pk=assessment.pk).update(
            reporting_manager_id=rm_id,
            skip_level_manager_id=skip_id,
            is_published=True
        )
        assessment_count += 1

        assessment_responses = list(
            responses.filter(assessment_id=assessment.pk)
            .select_related("parameter")
            .order_by("parameter_id", "id")
        )

        if not assessment_responses:
            empty_assessment_count += 1

        response_counts = Counter(
            (response.parameter_id, response.evaluator_type)
            for response in assessment_responses
        )

        parameter_map = {
            response.parameter_id: response.parameter
            for response in assessment_responses
        }

        item_map = {}

        # Reconstruct questionnaire items only from available response records.
        for order, parameter_id in enumerate(sorted(parameter_map), start=1):
            parameter = parameter_map[parameter_id]
            weightage = parameter.weightage

            if weightage is None or not isfinite(weightage) or weightage <= 0:
                invalid_weight_count += 1
                continue

            item, created = items.get_or_create(
                assessment_id=assessment.pk,
                parameter_id=parameter_id,
                defaults={
                    "weightage": weightage,
                    "order": order,
                    "source": "automatic",
                    "question_text_snapshot": parameter.question_text,
                    "question_type_snapshot": parameter.question_type,
                    "max_marks_snapshot": parameter.max_marks,
                    "category_name_snapshot": None,
                }
            )

            item_map[parameter_id] = item.pk

            if created:
                item_count += 1

        # Preserve all existing responses without violating uniqueness constraints.
        for response in assessment_responses:
            key = (response.parameter_id, response.evaluator_type)

            if response_counts[key] > 1:
                duplicate_response_count += 1
                continue

            item_id = item_map.get(response.parameter_id)

            if not item_id:
                continue

            if response.questionnaire_item_id is None:
                responses.filter(pk=response.pk).update(
                    questionnaire_item_id=item_id
                )
                linked_response_count += 1

        # Potential descriptive scoring remains independently disabled by default.
        _, created = descriptive_assessments.get_or_create(
            assessment_id=assessment.pk,
            defaults={
                "is_enabled": False,
                "status": "draft",
            }
        )

        if created:
            preference_count += 1

    print("\n" + "=" * 65)
    print("POTENTIAL LEGACY DATA MIGRATION COMPLETED")
    print("=" * 65)
    print(f"Legacy questions preserved: {legacy_question_count}")
    print(f"Existing assessments migrated: {assessment_count}")
    print(f"Questionnaire items reconstructed: {item_count}")
    print(f"Existing responses linked: {linked_response_count}")
    print(f"Duplicate responses preserved without links: {duplicate_response_count}")
    print(f"Invalid question weights skipped: {invalid_weight_count}")
    print(f"Assessments without existing responses: {empty_assessment_count}")
    print(f"Descriptive preferences initialized: {preference_count}")
    print("=" * 65 + "\n")


class Migration(migrations.Migration):

    dependencies = [
        ("scoring", "0009_potentialdepartmentassignment_and_more"),
    ]

    operations = [
        migrations.RunPython(backfill_potential_data),
    ]