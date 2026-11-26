from math import isfinite
from django.contrib.auth import get_user_model
from django.db import transaction
from django.db.models import Q, Count, Exists, OuterRef, Prefetch
from django.utils import timezone
from rest_framework.exceptions import ValidationError, NotFound, APIException
from evaluations.models import ReviewCycle, EmployeeQuestionnaire
from scoring.models import (
    PotentialParameter, EmployeePotentialAssessment,
    EmployeePotentialQuestionnaireItem, EmployeePotentialQuestionnaireCustomization,
    EmployeePotentialResponse, EmployeePotentialDescriptiveAssessment,
    EmployeePotentialDescriptiveAssessmentHistory, EmployeePotentialScore,
    EmployeeNineBoxPlacement, EmployeeAppraisalRecommendation,
    EmployeeAnalyticsRelease, EmployeePerformanceScore
)
from scoring.scoring_locks import lock_questionnaire, lock_questionnaires, lock_potential_assessment, lock_potential_assessments
from scoring.potential_question_bank.services import resolve_automatic_questions, question_bank_queryset

Employee = get_user_model()

class QuestionnaireConflict(APIException):
    status_code = 409
    default_detail = "The questionnaire has changed. Refresh it before saving again."
    default_code = "questionnaire_conflict"

class PotentialResetRequired(APIException):
    status_code = 409
    default_code = "potential_reset_required"

    def __init__(self, payload):
        self.detail = payload

def questionnaire_queryset():
    response_query = EmployeePotentialResponse.objects.filter(assessment_id=OuterRef("pk"))
    objective_filter = Q(items__question_type_snapshot="objective") | Q(items__question_type_snapshot__isnull=True, items__parameter__question_type="objective")
    descriptive_filter = Q(items__question_type_snapshot="descriptive") | Q(items__question_type_snapshot__isnull=True, items__parameter__question_type="descriptive")
    return EmployeePotentialAssessment.objects.select_related(
        "employee", "review_cycle", "employee_questionnaire", "reporting_manager", "skip_level_manager"
    ).annotate(
        question_count=Count("items", distinct=True),
        objective_count=Count("items", filter=objective_filter, distinct=True),
        descriptive_count=Count("items", filter=descriptive_filter, distinct=True),
        has_responses=Exists(response_query)
    ).order_by("-id")

def questionnaire_detail_queryset():
    return questionnaire_queryset().prefetch_related(
        Prefetch("items", queryset=EmployeePotentialQuestionnaireItem.objects.select_related(
            "parameter", "parameter__category", "assessment"
        ).order_by("order", "id")),
        "customizations"
    )

def get_questionnaire_detail(assessment_id):
    try:
        return questionnaire_detail_queryset().get(pk=assessment_id)
    except EmployeePotentialAssessment.DoesNotExist:
        raise NotFound("Potential questionnaire not found.")

def get_locked_assessment(assessment_id):
    _, assessment = lock_potential_assessment(assessment_id)
    return assessment

def validate_editable(assessment):
    if not assessment.employee.is_active:
        raise ValidationError({"message": "Cannot modify a questionnaire for an inactive employee."})

def validate_revision(assessment, revision):
    if assessment.updated_at != revision:
        raise QuestionnaireConflict({
            "message": "This questionnaire or its evaluation was modified elsewhere. Refresh before continuing.",
            "current_revision": assessment.updated_at.isoformat()
        })

def touch_assessment(assessment):
    assessment.save(update_fields=["updated_at"])

def current_items(assessment):
    return list(assessment.items.select_related("parameter", "parameter__category").order_by("order", "id"))

def get_customizations(assessment):
    return dict(assessment.customizations.values_list("parameter_id", "action"))

def get_automatic_questions(assessment):
    return list(resolve_automatic_questions(assessment.employee))

def lock_parameters(parameter_ids):
    return {
        parameter.pk: parameter
        for parameter in PotentialParameter.objects.select_for_update()
        .filter(pk__in=sorted(set(parameter_ids))).order_by("pk")
    }

def validate_weightage(weightage):
    if weightage is None or isinstance(weightage, bool):
        raise ValidationError({"weightage": "Every question must have a valid positive weightage."})
    try:
        valid = isfinite(weightage) and weightage > 0
    except (TypeError, ValueError, OverflowError):
        valid = False
    if not valid:
        raise ValidationError({"weightage": "Every question must have a valid positive weightage."})

def get_manager_ids(employee):
    rm_id = employee.reporting_manager_id
    skip_id = employee.skip_level_manager_id
    if rm_id == employee.pk:
        rm_id = None
    if skip_id == employee.pk:
        skip_id = None
    return rm_id, skip_id

def validate_managers(employee):
    if not employee.is_active:
        raise ValidationError("Cannot activate a questionnaire for an inactive employee.")
    rm_id, skip_id = get_manager_ids(employee)
    if not rm_id:
        raise ValidationError({"message": "The employee must have a Reporting Manager."})
    if skip_id and rm_id == skip_id:
        raise ValidationError({"message": "Reporting Manager and Skip-Level Manager must be different employees."})
    required_ids = {rm_id}
    if skip_id:
        required_ids.add(skip_id)
    active_ids = set(Employee.objects.filter(pk__in=required_ids, is_active=True).values_list("pk", flat=True))
    if active_ids != required_ids:
        raise ValidationError({"message": "All assigned managers must be active employees."})
    return rm_id, skip_id

def active_configuration(assessment):
    return {
        "items": [
            {"parameter_id": item.parameter_id, "weightage": item.weightage}
            for item in current_items(assessment)
        ],
        "excluded_parameter_ids": sorted(
            parameter_id for parameter_id, action in get_customizations(assessment).items()
            if action == "exclude"
        )
    }

def working_configuration(assessment):
    draft = assessment.customization_draft
    if draft is not None:
        return {
            "items": draft.get("items", []),
            "excluded_parameter_ids": draft.get("excluded_parameter_ids", [])
        }
    return active_configuration(assessment)

def configuration_signature(configuration):
    return (
        tuple((row["parameter_id"], float(row["weightage"])) for row in configuration["items"]),
        tuple(sorted(set(configuration.get("excluded_parameter_ids", []))))
    )

def replace_customizations(assessment, customizations, admin):
    existing = get_customizations(assessment)
    if existing == customizations:
        return False
    removed_ids = set(existing) - set(customizations)
    changed_ids = {
        parameter_id for parameter_id, action in customizations.items()
        if parameter_id in existing and existing[parameter_id] != action
    }
    if removed_ids or changed_ids:
        assessment.customizations.filter(parameter_id__in=removed_ids | changed_ids).delete()
    create_rows = [
        EmployeePotentialQuestionnaireCustomization(
            assessment_id=assessment.pk, parameter_id=parameter_id,
            action=action, created_by=admin
        )
        for parameter_id, action in sorted(customizations.items())
        if parameter_id not in existing or parameter_id in changed_ids
    ]
    if create_rows:
        EmployeePotentialQuestionnaireCustomization.objects.bulk_create(create_rows, batch_size=1000)
    return True

def validate_configuration(rows, excluded_ids, require_objective=False):
    desired_ids = [row["parameter_id"] for row in rows]
    excluded_ids = set(excluded_ids)
    if len(desired_ids) != len(set(desired_ids)):
        raise ValidationError("Duplicate questionnaire items are not allowed.")
    if set(desired_ids) & excluded_ids:
        raise ValidationError("A question cannot be included and excluded simultaneously.")
    parameters = lock_parameters(set(desired_ids) | excluded_ids)
    missing_ids = (set(desired_ids) | excluded_ids) - set(parameters)
    if missing_ids:
        raise ValidationError({"parameter_ids": f"Questions not found: {sorted(missing_ids)}"})
    objective_count = 0
    for row in rows:
        parameter = parameters[row["parameter_id"]]
        if not parameter.is_active:
            raise ValidationError({"parameter_id": parameter.pk, "message": "Question is inactive."})
        validate_weightage(row["weightage"])
        if parameter.question_type not in ("objective", "descriptive"):
            raise ValidationError({"parameter_id": parameter.pk, "message": "Unsupported question type."})
        if parameter.max_marks < 1:
            raise ValidationError({"parameter_id": parameter.pk, "message": "Invalid maximum marks."})
        if parameter.question_type == "objective":
            objective_count += 1
    if require_objective and not objective_count:
        raise ValidationError({"message": "At least one objective question is required."})
    return parameters

def persist_items(assessment, rows, parameters, automatic_ids):
    existing_items = {item.parameter_id: item for item in current_items(assessment)}
    desired_ids = {row["parameter_id"] for row in rows}
    removed_ids = set(existing_items) - desired_ids
    if removed_ids:
        assessment.items.filter(parameter_id__in=removed_ids).delete()
    create_rows, update_rows = [], []
    snapshot_fields = [
        "weightage", "order", "source", "question_text_snapshot",
        "question_type_snapshot", "max_marks_snapshot", "category_name_snapshot"
    ]
    for order, row in enumerate(rows, start=1):
        parameter = parameters[row["parameter_id"]]
        values = {
            "weightage": row["weightage"],
            "order": order,
            "source": "automatic" if parameter.pk in automatic_ids else "manual",
            "question_text_snapshot": parameter.question_text,
            "question_type_snapshot": parameter.question_type,
            "max_marks_snapshot": parameter.max_marks,
            "category_name_snapshot": parameter.category.name if parameter.category_id else None
        }
        existing = existing_items.get(parameter.pk)
        if existing:
            changed = any(getattr(existing, key) != value for key, value in values.items())
            if changed:
                for key, value in values.items():
                    setattr(existing, key, value)
                update_rows.append(existing)
        else:
            create_rows.append(EmployeePotentialQuestionnaireItem(
                assessment_id=assessment.pk, parameter_id=parameter.pk, **values
            ))
    if create_rows:
        EmployeePotentialQuestionnaireItem.objects.bulk_create(create_rows, batch_size=1000)
    if update_rows:
        EmployeePotentialQuestionnaireItem.objects.bulk_update(update_rows, snapshot_fields, batch_size=1000)
    return bool(removed_ids or create_rows or update_rows)

def save_working_configuration(assessment, rows, excluded_ids, admin):
    validate_configuration(rows, excluded_ids)
    configuration = {
        "items": [
            {"parameter_id": row["parameter_id"], "weightage": float(row["weightage"])}
            for row in rows
        ],
        "excluded_parameter_ids": sorted(set(excluded_ids))
    }
    if configuration_signature(configuration) == configuration_signature(working_configuration(assessment)):
        return False
    if configuration_signature(configuration) == configuration_signature(active_configuration(assessment)):
        assessment.customization_draft = None
    else:
        assessment.customization_draft = {
            **configuration,
            "saved_by": admin.pk,
            "saved_at": timezone.now().isoformat()
        }
    assessment.save(update_fields=["customization_draft", "updated_at"])
    return True

def automatic_configuration(assessment):
    questions = get_automatic_questions(assessment)
    return {
        "items": [
            {"parameter_id": question.pk, "weightage": question.weightage}
            for question in questions
        ],
        "excluded_parameter_ids": []
    }

def get_dependent_results(assessment):
    questionnaire_id = assessment.employee_questionnaire_id
    performance = EmployeePerformanceScore.objects.filter(employee_questionnaire_id=questionnaire_id)
    return {
        "potential_score": EmployeePotentialScore.objects.filter(employee_questionnaire_id=questionnaire_id).exists(),
        "ninebox": EmployeeNineBoxPlacement.objects.filter(employee_questionnaire_id=questionnaire_id).exists(),
        "appraisal": EmployeeAppraisalRecommendation.objects.filter(employee_questionnaire_id=questionnaire_id).exists(),
        "analytics_release": EmployeeAnalyticsRelease.objects.filter(
            employee_questionnaire_id=questionnaire_id, is_released_to_employee=True
        ).exists(),
        "cached_potential": performance.filter(potential_score__isnull=False).exists(),
        "cached_ninebox": performance.filter(nine_box_label__isnull=False).exists()
    }

def manager_payload(manager):
    if manager is None:
        return None
    return {
        "id": manager.pk,
        "employee_number": manager.employee_number,
        "full_name": manager.full_name
    }

def get_reset_requirements(assessment):
    responses = EmployeePotentialResponse.objects.filter(assessment_id=assessment.pk)
    counts = {
        row["evaluator_type"]: row["count"]
        for row in responses.values("evaluator_type").annotate(count=Count("pk"))
    }
    rm_count = counts.get("rm", 0)
    skip_count = counts.get("skip", 0)
    dependent_results = get_dependent_results(assessment)
    descriptive_calculated = EmployeePotentialDescriptiveAssessment.objects.filter(
        assessment_id=assessment.pk
    ).filter(Q(status="finalized") | Q(descriptive_score__isnull=False)).exists()
    return {
        "requires_reset": bool(
            rm_count or skip_count or assessment.rm_submitted or assessment.skip_submitted
            or descriptive_calculated or any(dependent_results.values())
        ),
        "response_count": rm_count + skip_count,
        "rm_response_count": rm_count,
        "skip_response_count": skip_count,
        "rm_submitted": assessment.rm_submitted,
        "skip_submitted": assessment.skip_submitted,
        "reporting_manager": manager_payload(assessment.reporting_manager),
        "skip_level_manager": manager_payload(assessment.skip_level_manager),
        "dependent_results": dependent_results,
        "descriptive_calculated": descriptive_calculated
    }

def reset_potential_evaluation(assessment, admin):
    questionnaire_id = assessment.employee_questionnaire_id
    response_count = EmployeePotentialResponse.objects.filter(assessment_id=assessment.pk).count()
    EmployeePotentialResponse.objects.filter(assessment_id=assessment.pk).delete()
    preference, _ = EmployeePotentialDescriptiveAssessment.objects.get_or_create(
        assessment_id=assessment.pk, defaults={"is_enabled": False}
    )
    previous_status = preference.status
    previous_score = preference.descriptive_score
    preference.status = "draft"
    preference.descriptive_score = None
    preference.objective_weight_used = None
    preference.descriptive_weight_used = None
    preference.assessed_by = None
    preference.assessed_at = None
    preference.save(update_fields=[
        "status", "descriptive_score", "objective_weight_used",
        "descriptive_weight_used", "assessed_by", "assessed_at", "updated_at"
    ])
    EmployeePotentialDescriptiveAssessmentHistory.objects.create(
        assessment=preference,
        action="questionnaire_republished",
        previous_status=previous_status,
        new_status="draft",
        previous_score=previous_score,
        new_score=None,
        change_details={
            "deleted_response_count": response_count,
            "descriptive_preference_preserved": True
        },
        changed_by=admin
    )
    EmployeeAppraisalRecommendation.objects.filter(employee_questionnaire_id=questionnaire_id).delete()
    EmployeeNineBoxPlacement.objects.filter(employee_questionnaire_id=questionnaire_id).delete()
    EmployeePotentialScore.objects.filter(employee_questionnaire_id=questionnaire_id).delete()
    EmployeePerformanceScore.objects.filter(employee_questionnaire_id=questionnaire_id).update(
        potential_score=None, nine_box_label=None
    )
    EmployeeAnalyticsRelease.objects.filter(
        employee_questionnaire_id=questionnaire_id, is_released_to_employee=True
    ).update(
        is_released_to_employee=False,
        released_at=None,
        released_by=None,
        unreleased_at=timezone.now(),
        unreleased_by=admin,
        updated_at=timezone.now()
    )
    assessment.rm_submitted = False
    assessment.skip_submitted = False
    assessment.rm_submitted_at = None
    assessment.skip_submitted_at = None
    assessment.status = "pending_rm"
    assessment.save(update_fields=[
        "rm_submitted", "skip_submitted", "rm_submitted_at",
        "skip_submitted_at", "status", "updated_at"
    ])

def has_active_configuration_changed(assessment, configuration, parameters, rm_id, skip_id):
    if not assessment.is_published:
        return True
    if configuration_signature(configuration) != configuration_signature(active_configuration(assessment)):
        return True
    if assessment.reporting_manager_id != rm_id or assessment.skip_level_manager_id != skip_id:
        return True
    for item in current_items(assessment):
        parameter = parameters[item.parameter_id]
        if (
            item.effective_question_text != parameter.question_text
            or item.effective_question_type != parameter.question_type
            or item.effective_max_marks != parameter.max_marks
        ):
            return True
    return False

def activate_configuration(assessment, configuration, admin=None, confirm_reset=False):
    rows = configuration["items"]
    excluded_ids = set(configuration.get("excluded_parameter_ids", []))
    parameters = validate_configuration(rows, excluded_ids, require_objective=True)
    employee = Employee.objects.get(pk=assessment.employee_id)
    rm_id, skip_id = validate_managers(employee)
    changed = has_active_configuration_changed(assessment, configuration, parameters, rm_id, skip_id)
    if not changed:
        if assessment.customization_draft is not None:
            assessment.customization_draft = None
            assessment.save(update_fields=["customization_draft", "updated_at"])
        return False
    reset_requirements = get_reset_requirements(assessment)
    if reset_requirements["requires_reset"]:
        if not confirm_reset or admin is None:
            raise PotentialResetRequired({
                "code": "potential_reset_required",
                "message": "Existing Potential evaluations must be reset before publishing the updated questionnaire.",
                "assessment_id": assessment.pk,
                "revision": assessment.updated_at.isoformat(),
                **reset_requirements
            })
        reset_potential_evaluation(assessment, admin)
    automatic_ids = {question.pk for question in get_automatic_questions(assessment)}
    desired_ids = {row["parameter_id"] for row in rows}
    customizations = {parameter_id: "include" for parameter_id in desired_ids - automatic_ids}
    customizations.update({parameter_id: "exclude" for parameter_id in excluded_ids})
    persist_items(assessment, rows, parameters, automatic_ids)
    replace_customizations(assessment, customizations, admin)
    assessment.reporting_manager_id = rm_id
    assessment.skip_level_manager_id = skip_id
    assessment.is_published = True
    assessment.published_at = timezone.now()
    assessment.published_by = admin
    assessment.status = "pending_rm"
    assessment.customization_draft = None
    assessment.save(update_fields=[
        "reporting_manager", "skip_level_manager", "is_published",
        "published_at", "published_by", "status", "customization_draft", "updated_at"
    ])
    EmployeePotentialDescriptiveAssessment.objects.get_or_create(
        assessment_id=assessment.pk, defaults={"is_enabled": False}
    )
    return True

@transaction.atomic
def ensure_default_assessment(questionnaire_id):
    questionnaire = lock_questionnaire(questionnaire_id)
    employee = Employee.objects.get(pk=questionnaire.employee_id)
    assessment = EmployeePotentialAssessment.objects.select_for_update().filter(
        employee_questionnaire_id=questionnaire.pk
    ).first()
    if assessment:
        if assessment.employee_id != questionnaire.employee_id or assessment.review_cycle_id != questionnaire.review_cycle_id:
            raise QuestionnaireConflict("Existing Potential assessment has inconsistent employee or review cycle information.")
        if assessment.is_published and assessment.items.exists():
            return assessment
        if assessment.rm_submitted or assessment.skip_submitted:
            return assessment
        if EmployeePotentialResponse.objects.filter(assessment_id=assessment.pk).exists():
            return assessment
    try:
        rm_id, skip_id = validate_managers(employee)
    except ValidationError:
        return assessment
    if assessment is None:
        assessment = EmployeePotentialAssessment.objects.create(
            employee_id=employee.pk,
            review_cycle_id=questionnaire.review_cycle_id,
            employee_questionnaire_id=questionnaire.pk,
            reporting_manager_id=rm_id,
            skip_level_manager_id=skip_id,
            status="draft", is_published=False
        )
    if get_dependent_results(assessment) and any(get_dependent_results(assessment).values()):
        return assessment
    configuration = automatic_configuration(assessment)
    if not configuration["items"]:
        return assessment
    parameters = lock_parameters([row["parameter_id"] for row in configuration["items"]])
    if not any(parameter.is_active and parameter.question_type == "objective" for parameter in parameters.values()):
        return assessment
    previous_draft = assessment.customization_draft
    if previous_draft is None and not assessment.is_published and assessment.items.exists():
        previous_draft = active_configuration(assessment)
    activate_configuration(assessment, configuration)
    if previous_draft is not None and configuration_signature(previous_draft) != configuration_signature(configuration):
        assessment.customization_draft = previous_draft
        assessment.save(update_fields=["customization_draft", "updated_at"])
    return assessment

@transaction.atomic
def create_questionnaire(employee_id, review_cycle_id, admin):
    try:
        employee = Employee.objects.get(pk=employee_id, is_active=True)
    except Employee.DoesNotExist:
        raise NotFound("Active employee not found.")
    try:
        cycle = ReviewCycle.objects.get(pk=review_cycle_id)
    except ReviewCycle.DoesNotExist:
        raise NotFound("Review cycle not found.")
    questionnaire, _ = EmployeeQuestionnaire.objects.get_or_create(
        employee_id=employee.pk, review_cycle_id=cycle.pk, defaults={"status": "draft"}
    )
    questionnaire = lock_questionnaire(questionnaire.pk)
    existing = EmployeePotentialAssessment.objects.filter(employee_questionnaire_id=questionnaire.pk).first()
    assessment = ensure_default_assessment(questionnaire.pk)
    if assessment is None:
        rm_id, skip_id = get_manager_ids(employee)
        assessment = EmployeePotentialAssessment.objects.create(
            employee_id=employee.pk, review_cycle_id=cycle.pk,
            employee_questionnaire_id=questionnaire.pk,
            reporting_manager_id=rm_id, skip_level_manager_id=skip_id,
            status="draft", is_published=False
        )
    EmployeePotentialDescriptiveAssessment.objects.get_or_create(
        assessment_id=assessment.pk, defaults={"is_enabled": False}
    )
    return get_questionnaire_detail(assessment.pk), existing is None

@transaction.atomic
def save_questionnaire_draft(assessment_id, revision, rows, excluded_ids, admin):
    assessment = get_locked_assessment(assessment_id)
    validate_editable(assessment)
    validate_revision(assessment, revision)
    automatic_ids = {question.pk for question in get_automatic_questions(assessment)}
    desired_ids = {row["parameter_id"] for row in rows}
    working = working_configuration(assessment)
    existing_ids = {row["parameter_id"] for row in working["items"]}
    all_excluded_ids = (
        ((automatic_ids & existing_ids) - desired_ids)
        | set(excluded_ids)
        | set(working["excluded_parameter_ids"])
    ) - desired_ids
    save_working_configuration(assessment, rows, all_excluded_ids, admin)
    return get_questionnaire_detail(assessment.pk)

@transaction.atomic
def synchronize_questionnaire(assessment_id, revision, admin):
    assessment = get_locked_assessment(assessment_id)
    validate_editable(assessment)
    validate_revision(assessment, revision)
    working = working_configuration(assessment)
    automatic_questions = get_automatic_questions(assessment)
    automatic_ids = {question.pk for question in automatic_questions}
    excluded_ids = set(working["excluded_parameter_ids"])
    existing_ids = {row["parameter_id"] for row in working["items"]}
    active_manual_ids = {
        item.parameter_id for item in current_items(assessment)
        if item.source == "manual"
    }
    included_ids = (existing_ids - automatic_ids) & active_manual_ids
    if assessment.customization_draft is not None:
        included_ids |= existing_ids - automatic_ids
    desired_ids = (automatic_ids | included_ids) - excluded_ids
    parameters = lock_parameters(desired_ids)
    desired_ids = {
        parameter_id for parameter_id in desired_ids
        if parameter_id in parameters and parameters[parameter_id].is_active
    }
    rows = [
        row for row in working["items"]
        if row["parameter_id"] in desired_ids
    ]
    retained_ids = {row["parameter_id"] for row in rows}
    for parameter_id in sorted(desired_ids - retained_ids):
        rows.append({"parameter_id": parameter_id, "weightage": parameters[parameter_id].weightage})
    save_working_configuration(assessment, rows, excluded_ids, admin)
    return get_questionnaire_detail(assessment.pk)

@transaction.atomic
def reset_questionnaire(assessment_id, revision, admin):
    assessment = get_locked_assessment(assessment_id)
    validate_editable(assessment)
    validate_revision(assessment, revision)
    configuration = automatic_configuration(assessment)
    save_working_configuration(assessment, configuration["items"], [], admin)
    return get_questionnaire_detail(assessment.pk)

@transaction.atomic
def copy_questionnaire(assessment_id, source_assessment_id, revision, copy_weightages, admin):
    if assessment_id == source_assessment_id:
        raise ValidationError("A questionnaire cannot be copied onto itself.")
    assessment_links = dict(EmployeePotentialAssessment.objects.filter(
        pk__in=[assessment_id, source_assessment_id]
    ).values_list("pk", "employee_questionnaire_id"))
    if len(assessment_links) != 2:
        raise NotFound("Source or destination questionnaire not found.")
    questionnaire_ids = set(assessment_links.values())
    locked_questionnaires = lock_questionnaires(questionnaire_ids)
    if set(locked_questionnaires) != questionnaire_ids:
        raise NotFound("Source or destination employee questionnaire not found.")
    assessments = {
        assessment.pk: assessment
        for assessment in lock_potential_assessments(questionnaire_ids)
    }
    assessment = assessments.get(assessment_id)
    source = assessments.get(source_assessment_id)
    if not assessment or not source:
        raise NotFound("Source or destination questionnaire not found.")
    validate_editable(assessment)
    validate_revision(assessment, revision)
    source_configuration = working_configuration(source)
    source_rows = source_configuration["items"]
    if not source_rows:
        raise ValidationError("The source questionnaire does not contain any questions.")
    source_ids = {row["parameter_id"] for row in source_rows}
    automatic_ids = {question.pk for question in get_automatic_questions(assessment)}
    parameters = lock_parameters(source_ids | automatic_ids)
    unavailable_ids = [
        parameter_id for parameter_id in source_ids
        if parameter_id not in parameters or not parameters[parameter_id].is_active
    ]
    if unavailable_ids:
        raise ValidationError({
            "message": "The source questionnaire contains unavailable or inactive questions.",
            "parameter_ids": sorted(unavailable_ids)
        })
    rows = [
        {
            "parameter_id": row["parameter_id"],
            "weightage": row["weightage"] if copy_weightages else parameters[row["parameter_id"]].weightage
        }
        for row in source_rows
    ]
    save_working_configuration(assessment, rows, automatic_ids - source_ids, admin)
    return get_questionnaire_detail(assessment.pk)

@transaction.atomic
def publish_questionnaire(assessment_id, revision, admin, confirm_reset=False):
    assessment = get_locked_assessment(assessment_id)
    validate_editable(assessment)
    validate_revision(assessment, revision)
    configuration = working_configuration(assessment)
    activate_configuration(assessment, configuration, admin=admin, confirm_reset=confirm_reset)
    return get_questionnaire_detail(assessment.pk)

def get_available_questions(assessment):
    automatic_ids = {question.pk for question in resolve_automatic_questions(assessment.employee)}
    configuration = working_configuration(assessment)
    assigned_ids = {row["parameter_id"] for row in configuration["items"]}
    excluded_ids = set(configuration["excluded_parameter_ids"])
    questions = list(question_bank_queryset().filter(Q(is_active=True) | Q(pk__in=assigned_ids)))
    return {
        "questions": questions,
        "automatic_parameter_ids": sorted(automatic_ids),
        "assigned_parameter_ids": sorted(assigned_ids),
        "excluded_parameter_ids": sorted(excluded_ids)
    }