import logging
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from django.utils import timezone
from django.db import transaction
from django.db.models import Case, When, Value, CharField
from employees.models import Employee
from evaluations.models import (
    EmployeeQuestionnaire, EmployeeQuestionnaireItem,
    EmployeeQuestionnaireStageSubmission, EmployeeQuestionnaireResponse,
    EmployeeQuestionnairePeerAssignment, EmployeeDescriptiveScore, ReviewCycle
)
from scoring.models import (
    EmployeePerformanceScore, EmployeePastPerformanceIndex,
    EmployeePotentialAssessment, EmployeePotentialResponse,
    EmployeePotentialScore, EmployeeNineBoxPlacement,
    EmployeeAppraisalRecommendation, EmployeeAnalyticsRelease,
    EmployeeDescriptiveAssessment, EmployeeDescriptiveQuestionRating,
    EmployeeDescriptiveAssessmentHistory, EmployeePotentialQuestionnaireItem,
    EmployeePotentialQuestionnaireCustomization, EmployeePotentialDescriptiveScore,
    EmployeePotentialDescriptiveAssessment, EmployeePotentialDescriptiveAssessmentHistory
)

logger = logging.getLogger(__name__)

def _is_admin(user):
    return user.is_authenticated and user.is_active and str(getattr(user, "employee_number", "")) == "100607"

def _valid_id(value):
    return value is not None and not isinstance(value, bool) and str(value).isdigit() and int(value) > 0

def _reset_querysets(employee, questionnaire_ids, cycle=None):
    ppi = EmployeePastPerformanceIndex.objects.filter(employee=employee)
    if cycle is not None:
        ppi = ppi.filter(review_cycle=cycle)
    return {
        "analytics_release_records": EmployeeAnalyticsRelease.objects.filter(
            employee_questionnaire_id__in=questionnaire_ids
        ),
        "appraisal_records": EmployeeAppraisalRecommendation.objects.filter(
            employee_questionnaire_id__in=questionnaire_ids
        ),
        "nine_box_records": EmployeeNineBoxPlacement.objects.filter(
            employee_questionnaire_id__in=questionnaire_ids
        ),
        "potential_scores": EmployeePotentialScore.objects.filter(
            employee_questionnaire_id__in=questionnaire_ids
        ),
        "performance_scores": EmployeePerformanceScore.objects.filter(
            employee_questionnaire_id__in=questionnaire_ids
        ),
        "ppi_records": ppi,
        "potential_descriptive_assessment_history": EmployeePotentialDescriptiveAssessmentHistory.objects.filter(
            assessment__assessment__employee_questionnaire_id__in=questionnaire_ids
        ),
        "potential_descriptive_scores": EmployeePotentialDescriptiveScore.objects.filter(
            response__assessment__employee_questionnaire_id__in=questionnaire_ids
        ),
        "potential_descriptive_assessments": EmployeePotentialDescriptiveAssessment.objects.filter(
            assessment__employee_questionnaire_id__in=questionnaire_ids
        ),
        "potential_responses": EmployeePotentialResponse.objects.filter(
            assessment__employee_questionnaire_id__in=questionnaire_ids
        ),
        "descriptive_assessment_history": EmployeeDescriptiveAssessmentHistory.objects.filter(
            assessment__employee_questionnaire_id__in=questionnaire_ids
        ),
        "descriptive_question_ratings": EmployeeDescriptiveQuestionRating.objects.filter(
            assessment__employee_questionnaire_id__in=questionnaire_ids
        ),
        "descriptive_assessments": EmployeeDescriptiveAssessment.objects.filter(
            employee_questionnaire_id__in=questionnaire_ids
        ),
        "descriptive_scores": EmployeeDescriptiveScore.objects.filter(
            response__submission__employee_questionnaire_id__in=questionnaire_ids
        ),
        "questionnaire_responses": EmployeeQuestionnaireResponse.objects.filter(
            submission__employee_questionnaire_id__in=questionnaire_ids
        ),
        "stage_submissions": EmployeeQuestionnaireStageSubmission.objects.filter(
            employee_questionnaire_id__in=questionnaire_ids
        ),
        "peer_assignments": EmployeeQuestionnairePeerAssignment.objects.filter(
            employee_questionnaire_id__in=questionnaire_ids
        )
    }

def _preserved_summary(questionnaire_ids):
    return {
        "questionnaires": len(questionnaire_ids),
        "questionnaire_items": EmployeeQuestionnaireItem.objects.filter(
            employee_questionnaire_id__in=questionnaire_ids
        ).count(),
        "potential_assessments": EmployeePotentialAssessment.objects.filter(
            employee_questionnaire_id__in=questionnaire_ids
        ).count(),
        "potential_questionnaire_items": EmployeePotentialQuestionnaireItem.objects.filter(
            assessment__employee_questionnaire_id__in=questionnaire_ids
        ).count(),
        "potential_questionnaire_customizations": EmployeePotentialQuestionnaireCustomization.objects.filter(
            assessment__employee_questionnaire_id__in=questionnaire_ids
        ).count()
    }

def _reset_potential_assessments(questionnaire_ids):
    return EmployeePotentialAssessment.objects.filter(
        employee_questionnaire_id__in=questionnaire_ids
    ).update(
        rm_submitted=False,
        skip_submitted=False,
        rm_submitted_at=None,
        skip_submitted_at=None,
        status=Case(
            When(is_published=True, then=Value("pending_rm")),
            default=Value("draft"),
            output_field=CharField()
        ),
        updated_at=timezone.now()
    )

def _get_employee_and_cycle(employee_number, review_cycle_id, scope):
    if not employee_number:
        return None, None, Response({"error": "employee_number is required"}, status=400)
    if scope not in ("cycle", "all"):
        return None, None, Response({"error": "Invalid reset scope"}, status=400)
    employee = Employee.objects.filter(employee_number=employee_number).first()
    if not employee:
        return None, None, Response({"error": "Employee not found"}, status=404)
    cycle = None
    if scope == "cycle":
        if not review_cycle_id:
            return None, None, Response({"error": "review_cycle_id is required for cycle reset"}, status=400)
        if not _valid_id(review_cycle_id):
            return None, None, Response({"error": "Invalid review cycle"}, status=400)
        cycle = ReviewCycle.objects.filter(id=review_cycle_id).first()
        if not cycle:
            return None, None, Response({"error": "Invalid review cycle"}, status=404)
    return employee, cycle, None

def _get_questionnaires(employee, cycle=None):
    questionnaires = EmployeeQuestionnaire.objects.filter(employee=employee)
    if cycle is not None:
        questionnaires = questionnaires.filter(review_cycle=cycle)
    return questionnaires

class EmployeeResetPreviewView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        if not _is_admin(request.user):
            return Response({"error": "Not authorized"}, status=403)
        employee_number = request.GET.get("employee_number")
        review_cycle_id = request.GET.get("review_cycle_id")
        scope = request.GET.get("scope", "cycle")
        employee, cycle, error = _get_employee_and_cycle(employee_number, review_cycle_id, scope)
        if error is not None:
            return error
        questionnaires = _get_questionnaires(employee, cycle)
        questionnaire_ids = list(questionnaires.values_list("id", flat=True))
        response_data = {
            "employee": {
                "id": employee.id,
                "employee_number": employee.employee_number,
                "full_name": getattr(employee, "full_name", str(employee)),
                "department": getattr(employee, "department", None)
            },
            "review_cycle": None,
            "questionnaire": None,
            "potential_assessment": None,
            "delete_summary": {},
            "preserved_summary": {},
            "reset_summary": {},
            "safe_to_reset": False,
            "warnings": []
        }
        if cycle is not None:
            response_data["review_cycle"] = {"id": cycle.id, "name": cycle.name}
            questionnaire = questionnaires.first()
            if questionnaire:
                response_data["questionnaire"] = {
                    "id": questionnaire.id,
                    "status": questionnaire.status,
                    "submitted_at": questionnaire.submitted_at
                }
                potential = EmployeePotentialAssessment.objects.filter(
                    employee_questionnaire_id=questionnaire.pk
                ).first()
                if potential:
                    response_data["potential_assessment"] = {
                        "id": potential.pk,
                        "status": potential.status,
                        "is_published": potential.is_published,
                        "rm_submitted": potential.rm_submitted,
                        "skip_submitted": potential.skip_submitted,
                        "status_after_reset": "pending_rm" if potential.is_published else "draft"
                    }
        preserved = _preserved_summary(questionnaire_ids)
        querysets = _reset_querysets(employee, questionnaire_ids, cycle)
        summary = {name: queryset.count() for name, queryset in querysets.items()}
        response_data["delete_summary"] = {
            "questionnaires": preserved["questionnaires"],
            "questionnaire_items": preserved["questionnaire_items"],
            "potential_assessments": 0,
            **summary
        }
        response_data["preserved_summary"] = preserved
        response_data["reset_summary"] = {
            "questionnaires_reset": preserved["questionnaires"],
            "potential_assessments_reset": preserved["potential_assessments"],
            "performance_descriptive_preferences_reset": summary["descriptive_assessments"],
            "potential_descriptive_preferences_reset": summary["potential_descriptive_assessments"]
        }
        response_data["safe_to_reset"] = bool(questionnaire_ids or any(summary.values()))
        if not response_data["safe_to_reset"]:
            response_data["warnings"].append(
                "No PMS data found for this employee."
                if scope == "all"
                else "No PMS data found for this employee in the selected cycle."
            )
        else:
            response_data["warnings"].append(
                "Evaluation responses, descriptive marks, calculated scores, overrides and analytics releases will be removed."
            )
            response_data["warnings"].append(
                "Performance and Potential questionnaire structures, publication states and question-bank configurations will be preserved."
            )
            response_data["warnings"].append(
                "Both Performance and Potential descriptive scoring preferences will return to OFF."
            )
            if cycle is not None and EmployeePastPerformanceIndex.objects.filter(
                employee_id=employee.pk, review_cycle_id__gt=cycle.pk
            ).exists():
                response_data["warnings"].append(
                    "Later review-cycle PPI and dependent analytics may require recalculation because historical Performance inputs are changing."
                )
        return Response(response_data)

class EmployeeResetExecuteView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        if not _is_admin(request.user):
            return Response({"error": "Not authorized"}, status=403)
        employee_number = request.data.get("employee_number")
        review_cycle_id = request.data.get("review_cycle_id")
        scope = request.data.get("scope", "cycle")
        reason = request.data.get("reason", "")
        employee, cycle, error = _get_employee_and_cycle(employee_number, review_cycle_id, scope)
        if error is not None:
            return error
        try:
            with transaction.atomic():
                questionnaires = _get_questionnaires(employee, cycle)
                questionnaire_ids = list(
                    questionnaires.select_for_update().order_by("id").values_list("id", flat=True)
                )
                list(EmployeePotentialAssessment.objects.select_for_update().filter(
                    employee_questionnaire_id__in=questionnaire_ids
                ).order_by("id").values_list("id", flat=True))
                questionnaires = EmployeeQuestionnaire.objects.filter(id__in=questionnaire_ids)
                preserved = _preserved_summary(questionnaire_ids)
                querysets = _reset_querysets(employee, questionnaire_ids, cycle)
                deleted = {
                    "questionnaires_reset": len(questionnaire_ids),
                    "questionnaire_items_kept": preserved["questionnaire_items"],
                    "potential_assessments": 0,
                    "potential_assessments_kept": preserved["potential_assessments"],
                    "potential_questionnaire_items_kept": preserved["potential_questionnaire_items"],
                    "potential_questionnaire_customizations_kept": preserved["potential_questionnaire_customizations"]
                }
                deleted.update({name: queryset.count() for name, queryset in querysets.items()})
                for queryset in querysets.values():
                    queryset.delete()
                questionnaires.update(
                    status="draft",
                    submitted_at=None,
                    peer_reviewer=None,
                    peer_requested_at=None
                )
                potential_reset_count = _reset_potential_assessments(questionnaire_ids)
                deleted["potential_assessments_reset"] = potential_reset_count
                later_cycles = []
                if cycle is not None:
                    later_cycles = list(EmployeePastPerformanceIndex.objects.filter(
                        employee_id=employee.pk,
                        review_cycle_id__gt=cycle.pk
                    ).order_by("review_cycle_id").values_list("review_cycle_id", flat=True).distinct())
                return Response({
                    "message": "PMS data reset successfully",
                    "employee_number": employee_number,
                    "scope": scope,
                    "review_cycle_id": review_cycle_id if scope != "all" else None,
                    "reason": reason,
                    "deleted": deleted,
                    "preserved": preserved,
                    "reset_summary": {
                        "questionnaires_reset": len(questionnaire_ids),
                        "potential_assessments_reset": potential_reset_count,
                        "performance_descriptive_preferences_reset": deleted["descriptive_assessments"],
                        "potential_descriptive_preferences_reset": deleted["potential_descriptive_assessments"]
                    },
                    "descriptive_preferences": {
                        "performance_enabled": False,
                        "potential_enabled": False
                    },
                    "later_review_cycles_requiring_verification": later_cycles,
                    "timestamp": timezone.now()
                })
        except Exception:
            logger.exception("PMS reset failed for employee %s, scope %s, cycle %s", employee_number, scope, review_cycle_id)
            return Response({
                "error": "Reset failed",
                "details": "The reset transaction could not be completed. No partial reset has been committed."
            }, status=500)

class AdminReviewCycleListView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        if not _is_admin(request.user):
            return Response({"error": "Not authorized"}, status=403)
        cycles = ReviewCycle.objects.all().order_by("-id")
        data = [{
            "id": cycle.id,
            "name": cycle.name,
            "start_date": cycle.start_date,
            "end_date": cycle.end_date,
            "is_active": cycle.is_active
        } for cycle in cycles]
        return Response({"results": data}, status=200)