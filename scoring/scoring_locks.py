from django.db import connection
from rest_framework.exceptions import NotFound
from evaluations.models import EmployeeQuestionnaire
from .models import EmployeePotentialAssessment

def require_atomic_transaction():
    if not connection.in_atomic_block:
        raise RuntimeError("Scoring locks require an active database transaction.")

def lock_questionnaire(questionnaire_id):
    require_atomic_transaction()
    try:
        return EmployeeQuestionnaire.objects.select_for_update().get(pk=questionnaire_id)
    except EmployeeQuestionnaire.DoesNotExist:
        raise NotFound("Employee questionnaire not found.")

def lock_questionnaires(questionnaire_ids):
    require_atomic_transaction()
    return {
        questionnaire.pk: questionnaire
        for questionnaire in EmployeeQuestionnaire.objects.select_for_update().filter(
            pk__in=set(questionnaire_ids)
        ).order_by("pk")
    }

def lock_potential_assessment(assessment_id):
    require_atomic_transaction()
    questionnaire_id = EmployeePotentialAssessment.objects.filter(
        pk=assessment_id
    ).values_list("employee_questionnaire_id", flat=True).first()
    if questionnaire_id is None:
        raise NotFound("Potential assessment not found.")
    questionnaire = lock_questionnaire(questionnaire_id)
    try:
        assessment = EmployeePotentialAssessment.objects.select_for_update().get(
            pk=assessment_id, employee_questionnaire_id=questionnaire.pk
        )
    except EmployeePotentialAssessment.DoesNotExist:
        raise NotFound("Potential assessment not found.")
    return questionnaire, assessment

def lock_potential_assessments(questionnaire_ids):
    require_atomic_transaction()
    return list(EmployeePotentialAssessment.objects.select_for_update().filter(
        employee_questionnaire_id__in=questionnaire_ids
    ).order_by("pk"))