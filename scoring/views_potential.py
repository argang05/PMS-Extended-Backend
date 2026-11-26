from django.db import transaction
from django.shortcuts import get_object_or_404
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from rest_framework.exceptions import ValidationError, PermissionDenied
from .models import EmployeePotentialScore
from .scoring_locks import lock_potential_assessment
from .services_potential import get_active_potential_config, calculate_weighted_potential_score, save_potential_score, serialize_potential_score
from .potential_evaluation.services import (
    manager_assessment_queryset, get_assessment, resolve_evaluator,
    evaluator_can_submit, evaluator_has_submitted, required_evaluators,
    evaluation_is_complete, get_assessment_items, materialize_legacy_items,
    build_evaluation_detail, save_evaluation, positive_id, is_legacy_assessment
)

def is_scoring_admin(user):
    return user.is_authenticated and user.is_active and str(getattr(user, "employee_number", "")) == "100607"

def _require_active_user(user):
    if not user.is_authenticated or not user.is_active:
        raise PermissionDenied("An active employee account is required.")

def _require_evaluator(assessment, user, evaluator):
    _require_active_user(user)
    if resolve_evaluator(assessment, user) != evaluator:
        raise PermissionDenied("You are not authorized for this evaluation stage.")

def _pending_assessments(user, evaluator):
    _require_active_user(user)
    queryset = manager_assessment_queryset(user)
    if evaluator == "rm":
        queryset = queryset.filter(reporting_manager_id=user.pk)
    else:
        queryset = queryset.filter(skip_level_manager_id=user.pk, rm_submitted=True)
    return [{
        "assessment_id": assessment.pk,
        "questionnaire_id": assessment.employee_questionnaire_id,
        "employee_questionnaire_id": assessment.employee_questionnaire_id,
        "employee_name": assessment.employee.full_name,
        "employee_number": assessment.employee.employee_number,
        "department": assessment.employee.department,
        "cycle": assessment.review_cycle.name if assessment.review_cycle else None,
        "review_cycle_id": assessment.review_cycle_id,
        "status": assessment.status,
        "rm_submitted": assessment.rm_submitted,
        "skip_submitted": assessment.skip_submitted,
        "skip_required": "skip" in required_evaluators(assessment),
        "evaluation_complete": evaluation_is_complete(assessment),
        "is_published": assessment.is_published,
        "question_count": assessment.question_count,
        "can_evaluate": evaluator_can_submit(assessment, evaluator) and (
            assessment.question_count > 0 or is_legacy_assessment(assessment)
        ),
        "is_submitted": evaluator_has_submitted(assessment, evaluator),
        "requires_preparation": assessment.question_count == 0 and is_legacy_assessment(assessment)
            and evaluator_can_submit(assessment, evaluator),
        "revision": assessment.updated_at.isoformat()
    } for assessment in queryset]

@transaction.atomic
def _evaluation_form(assessment_id, user, evaluator):
    assessment = get_assessment(assessment_id, lock=True)
    _require_evaluator(assessment, user, evaluator)
    if evaluator == "skip" and not assessment.rm_submitted:
        raise ValidationError("The Reporting Manager must complete the evaluation first.")
    items = get_assessment_items(assessment)
    if not items and evaluator_can_submit(assessment, evaluator) and is_legacy_assessment(assessment):
        items = materialize_legacy_items(assessment)
    detail = build_evaluation_detail(assessment, evaluator, items)
    responses = detail["responses"]
    return {
        "assessment_id": assessment.pk,
        "questionnaire_id": assessment.employee_questionnaire_id,
        "employee_questionnaire_id": assessment.employee_questionnaire_id,
        "employee_name": assessment.employee.full_name,
        "employee_number": assessment.employee.employee_number,
        "department": assessment.employee.department,
        "cycle": assessment.review_cycle.name if assessment.review_cycle else None,
        "review_cycle_id": assessment.review_cycle_id,
        "status": detail["status"],
        "evaluator_type": evaluator,
        "is_published": detail["is_published"],
        "is_submitted": detail["is_submitted"],
        "can_evaluate": detail["can_evaluate"],
        "rm_submitted": detail["rm_submitted"],
        "skip_submitted": detail["skip_submitted"],
        "skip_required": detail["skip_required"],
        "required_evaluators": detail["required_evaluators"],
        "evaluation_complete": detail["evaluation_complete"],
        "question_count": detail["question_count"],
        "objective_count": detail["objective_count"],
        "descriptive_count": detail["descriptive_count"],
        "requires_preparation": detail["requires_preparation"],
        "revision": detail["revision"],
        "questions": detail["questions"],
        "responses": {response["parameter_id"]: response["score"] for response in responses},
        "descriptive_responses": {
            response["parameter_id"]: response["descriptive_answer"]
            for response in responses if response["descriptive_answer"] is not None
        },
        "response_details": responses
    }

def _save_evaluation(user, evaluator, payload, submit=False):
    _require_active_user(user)
    assessment_id = positive_id(payload.get("assessment_id"), "assessment_id")
    detail = save_evaluation(
        assessment_id=assessment_id, user=user,
        responses=payload.get("responses", []),
        revision=payload.get("revision"), submit=submit,
        expected_evaluator=evaluator, allow_legacy_preparation=True
    )
    label = "RM" if evaluator == "rm" else "Skip"
    return {
        "message": f"{label} potential questionnaire submitted." if submit else f"{label} potential draft saved.",
        "assessment_id": detail["assessment_id"],
        "status": detail["status"],
        "rm_submitted": detail["rm_submitted"],
        "skip_submitted": detail["skip_submitted"],
        "skip_required": detail["skip_required"],
        "evaluation_complete": detail["evaluation_complete"],
        "revision": detail["revision"],
        "is_submitted": detail["is_submitted"],
        "changed": detail["changed"]
    }

class PotentialAPIView(APIView):
    permission_classes = [IsAuthenticated]

class RMPotentialPendingView(PotentialAPIView):
    def get(self, request):
        return Response({"results": _pending_assessments(request.user, "rm")})

class SkipPotentialPendingView(PotentialAPIView):
    def get(self, request):
        return Response({"results": _pending_assessments(request.user, "skip")})

class RMPotentialFormView(PotentialAPIView):
    def get(self, request, assessment_id):
        return Response(_evaluation_form(assessment_id, request.user, "rm"))

class SkipPotentialFormView(PotentialAPIView):
    def get(self, request, assessment_id):
        return Response(_evaluation_form(assessment_id, request.user, "skip"))

class RMSavePotentialDraftView(PotentialAPIView):
    def post(self, request):
        return Response(_save_evaluation(request.user, "rm", request.data))

class SkipSavePotentialDraftView(PotentialAPIView):
    def post(self, request):
        return Response(_save_evaluation(request.user, "skip", request.data))

class RMSubmitPotentialView(PotentialAPIView):
    def post(self, request):
        return Response(_save_evaluation(request.user, "rm", request.data, submit=True))

class SkipSubmitPotentialView(PotentialAPIView):
    def post(self, request):
        return Response(_save_evaluation(request.user, "skip", request.data, submit=True))

class CalculatePotentialScoreView(PotentialAPIView):
    def post(self, request, assessment_id):
        if not is_scoring_admin(request.user):
            return Response({"error": "Not authorized."}, status=403)
        assessment, score, changed, invalidation = save_potential_score(assessment_id, request.user)
        return Response({
            "message": "Potential score calculated successfully.",
            **serialize_potential_score(assessment, score),
            "changed": changed, "invalidation": invalidation
        })

class PotentialResultView(PotentialAPIView):
    @transaction.atomic
    def get(self, request, assessment_id):
        if not is_scoring_admin(request.user):
            return Response({"error": "Not authorized."}, status=403)
        questionnaire, assessment = lock_potential_assessment(assessment_id)
        score = get_object_or_404(
            EmployeePotentialScore,
            employee_id=assessment.employee_id,
            review_cycle_id=assessment.review_cycle_id,
            employee_questionnaire_id=questionnaire.pk
        )
        from .score_readiness import get_potential_readiness
        readiness = get_potential_readiness(questionnaire, score=score)
        return Response({
            **serialize_potential_score(assessment, score),
            "is_current": readiness["is_current"],
            "missing_requirements": readiness["missing_requirements"]
        })