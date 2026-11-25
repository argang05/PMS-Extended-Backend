from math import isclose
from django.db import transaction
from django.utils import timezone
from django.shortcuts import get_object_or_404
from django.core.exceptions import ValidationError as DjangoValidationError
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from rest_framework.exceptions import ValidationError
from evaluations.models import EmployeeQuestionnaire
from .models import (
    EmployeePerformanceScore, EmployeePastPerformanceIndex,
    EmployeePotentialAssessment, EmployeeAnalyticsRelease
)
from .serializers import (
    EmployeePerformanceScoreSerializer, HRScoreOverrideSerializer,
    ReleaseScoreSerializer
)
from .services import get_active_config, calculate_questionnaire_scores
from .services_ppi import get_past_scores, save_ppi


def is_scoring_admin(user):
    return str(getattr(user, "employee_number", "")) == "100607"


def _same(a, b):
    if a is None or b is None:
        return a is None and b is None
    return isclose(float(a), float(b), rel_tol=0, abs_tol=0.011)


def _calculation(questionnaire):
    config = get_active_config()
    if not config:
        raise ValidationError({"error": "No active scoring configuration found."})
    try:
        return calculate_questionnaire_scores(questionnaire, config)
    except DjangoValidationError as exc:
        raise ValidationError({"error": exc.messages})


def _current_score(questionnaire, score):
    if not score or score.system_final_score is None:
        return False

    result = _calculation(questionnaire)
    if not result["is_complete"]:
        return False

    if not _same(score.system_final_score, result["system_final_score"]):
        return False

    if score.descriptive_scoring_applied != result["descriptive_scoring_applied"]:
        return False

    if not _same(score.objective_final_score, result["objective_final_score"]):
        return False

    if not _same(score.descriptive_final_score, result["descriptive_final_score"]):
        return False

    expected = (
        score.hr_override_score
        if score.hr_override_score is not None
        else score.system_final_score
    )
    return _same(score.final_effective_score, expected)


def _category_score(result, evaluator, category):
    objective = result["category_scores"].get(f"{evaluator}_{category}_objective")
    descriptive = result["category_scores"].get(f"{evaluator}_{category}_descriptive")
    ow = result["effective_objective_component_weight"]
    dw = result["effective_descriptive_component_weight"]

    if ow > 0 and objective is None:
        return None
    if dw > 0 and descriptive is None:
        return None

    return round((objective or 0) * ow + (descriptive or 0) * dw, 2)


def _invalidate_analytics(questionnaire):
    EmployeeAnalyticsRelease.objects.filter(
        employee_questionnaire=questionnaire
    ).update(
        is_released_to_employee=False,
        released_at=None,
        released_by=None
    )


class AdminScoringQuestionnaireListView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        if not is_scoring_admin(request.user):
            return Response({"error": "Not authorized."}, status=403)

        questionnaires = EmployeeQuestionnaire.objects.select_related(
            "employee", "review_cycle"
        ).order_by("-id")

        data = []

        for questionnaire in questionnaires:
            score = EmployeePerformanceScore.objects.filter(
                employee_questionnaire=questionnaire
            ).first()

            potential = EmployeePotentialAssessment.objects.filter(
                employee_questionnaire=questionnaire
            ).first()

            data.append({
                "questionnaire_id": questionnaire.id,
                "employee_id": questionnaire.employee_id,
                "employee_name": questionnaire.employee.full_name,
                "employee_number": questionnaire.employee.employee_number,
                "department": questionnaire.employee.department,
                "review_cycle": questionnaire.review_cycle.name,
                "status": questionnaire.status,
                "has_score": bool(score),
                "system_final_score": score.system_final_score if score else None,
                "final_effective_score": score.final_effective_score if score else None,
                "is_released_to_employee": score.is_released_to_employee if score else False,
                "has_potential_assessment": bool(potential),
                "assessment_id": potential.id if potential else None
            })

        return Response(data)


class CalculateScoreView(APIView):
    permission_classes = [IsAuthenticated]

    @transaction.atomic
    def post(self, request, questionnaire_id):
        if not is_scoring_admin(request.user):
            return Response({"error": "Not authorized."}, status=403)

        # FIX: Lock only the EmployeeQuestionnaire row.
        # Nullable reporting_manager joins must not be locked.
        questionnaire = get_object_or_404(
            EmployeeQuestionnaire.objects.select_related(
                "employee",
                "employee__reporting_manager",
                "review_cycle"
            ).select_for_update(of=("self",)),
            id=questionnaire_id
        )

        result = _calculation(questionnaire)

        if not result["is_complete"]:
            return Response({
                "error": "Performance scoring is incomplete.",
                "missing_requirements": result["missing_requirements"]
            }, status=400)

        score, _ = EmployeePerformanceScore.objects.select_for_update().get_or_create(
            employee_questionnaire=questionnaire,
            defaults={
                "employee": questionnaire.employee,
                "review_cycle": questionnaire.review_cycle
            }
        )

        old_system = score.system_final_score
        old_effective = score.final_effective_score

        score.employee = questionnaire.employee
        score.review_cycle = questionnaire.review_cycle

        for evaluator in ("self", "rm", "skip", "peer"):
            setattr(
                score,
                f"{evaluator}_behavioural_score",
                _category_score(result, evaluator, "behavioural")
            )
            setattr(
                score,
                f"{evaluator}_performance_score",
                _category_score(result, evaluator, "performance")
            )
            setattr(
                score,
                f"{evaluator}_score",
                result["evaluator_scores"].get(evaluator)
            )

        weights = result["effective_weights"]
        effective = next(iter(weights.values()), {})

        for evaluator in ("self", "rm", "skip", "peer"):
            setattr(
                score,
                f"effective_{evaluator}_weight",
                effective.get(evaluator, 0)
            )

        score.objective_final_score = result["objective_final_score"]
        score.descriptive_final_score = result["descriptive_final_score"]
        score.descriptive_scoring_applied = result["descriptive_scoring_applied"]
        score.effective_objective_component_weight = result["effective_objective_component_weight"]
        score.effective_descriptive_component_weight = result["effective_descriptive_component_weight"]
        score.system_final_score = result["system_final_score"]

        score.final_effective_score = (
            score.hr_override_score
            if score.hr_override_score is not None
            else score.system_final_score
        )

        changed = (
            not _same(old_system, score.system_final_score)
            or not _same(old_effective, score.final_effective_score)
        )

        if changed:
            score.is_released_to_employee = False
            score.released_by = None
            score.released_at = None
            _invalidate_analytics(questionnaire)

        score.save()

        return Response(EmployeePerformanceScoreSerializer(score).data)


class ScoreResultView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, questionnaire_id):
        if not is_scoring_admin(request.user):
            return Response({"error": "Not authorized."}, status=403)

        score = get_object_or_404(
            EmployeePerformanceScore,
            employee_questionnaire_id=questionnaire_id
        )

        return Response(EmployeePerformanceScoreSerializer(score).data)


class HRScoreOverrideView(APIView):
    permission_classes = [IsAuthenticated]

    @transaction.atomic
    def put(self, request, questionnaire_id):
        if not is_scoring_admin(request.user):
            return Response({"error": "Not authorized."}, status=403)

        # FIX: Lock only EmployeePerformanceScore, not nullable joins.
        score = get_object_or_404(
            EmployeePerformanceScore.objects.select_related(
                "employee_questionnaire__employee__reporting_manager"
            ).select_for_update(of=("self",)),
            employee_questionnaire_id=questionnaire_id
        )

        if not _current_score(score.employee_questionnaire, score):
            return Response({
                "error": "Performance score is incomplete or outdated. Recalculate it first."
            }, status=409)

        serializer = HRScoreOverrideSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        override = serializer.validated_data["hr_override_score"]
        reason = serializer.validated_data["override_reason"].strip()

        if not 0 <= override <= 10:
            return Response({
                "error": "Override score must be between 0 and 10."
            }, status=400)

        if not reason:
            return Response({"error": "Override reason is required."}, status=400)

        override = round(override, 2)
        changed = not _same(score.hr_override_score, override)

        score.hr_override_score = override
        score.final_effective_score = override
        score.override_reason = reason
        score.overridden_by = request.user
        score.overridden_at = timezone.now()

        if changed:
            score.is_released_to_employee = False
            score.released_by = None
            score.released_at = None
            _invalidate_analytics(score.employee_questionnaire)

        score.save()

        return Response(EmployeePerformanceScoreSerializer(score).data)


class ReleaseScoreView(APIView):
    permission_classes = [IsAuthenticated]

    @transaction.atomic
    def put(self, request, questionnaire_id):
        if not is_scoring_admin(request.user):
            return Response({"error": "Not authorized."}, status=403)

        # FIX: Avoid PostgreSQL FOR UPDATE on nullable reporting_manager join.
        score = get_object_or_404(
            EmployeePerformanceScore.objects.select_related(
                "employee_questionnaire__employee__reporting_manager"
            ).select_for_update(of=("self",)),
            employee_questionnaire_id=questionnaire_id
        )

        serializer = ReleaseScoreSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        release = serializer.validated_data["is_released_to_employee"]

        if release and not _current_score(score.employee_questionnaire, score):
            return Response({
                "error": "Score cannot be released because its calculation is incomplete or outdated."
            }, status=409)

        score.is_released_to_employee = release
        score.released_by = request.user if release else None
        score.released_at = timezone.now() if release else None

        score.save(update_fields=[
            "is_released_to_employee",
            "released_by",
            "released_at",
            "calculated_at"
        ])

        return Response(EmployeePerformanceScoreSerializer(score).data)


class MyScoreView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        score = EmployeePerformanceScore.objects.filter(
            employee=request.user,
            is_released_to_employee=True
        ).select_related(
            "employee_questionnaire__employee__reporting_manager",
            "review_cycle"
        ).order_by("-review_cycle_id", "-id").first()

        if not score:
            return Response({
                "message": "Your score has not been released yet."
            }, status=403)

        if not _current_score(score.employee_questionnaire, score):
            return Response({
                "error": "Your score is temporarily unavailable because it requires recalculation."
            }, status=409)

        return Response({
            "system_final_score": score.system_final_score,
            "final_effective_score": score.final_effective_score,
            "potential_score": score.potential_score,
            "nine_box_label": score.nine_box_label
        })


class CalculatePPIView(APIView):
    permission_classes = [IsAuthenticated]

    @transaction.atomic
    def post(self, request, questionnaire_id):
        if not is_scoring_admin(request.user):
            return Response({"error": "Not authorized."}, status=403)

        score = get_object_or_404(
            EmployeePerformanceScore.objects.select_related(
                "employee_questionnaire__employee__reporting_manager",
                "review_cycle",
                "employee"
            ),
            employee_questionnaire_id=questionnaire_id
        )

        if not _current_score(score.employee_questionnaire, score):
            return Response({
                "error": "Recalculate the current performance score before calculating PPI."
            }, status=409)

        past_scores = get_past_scores(score.employee, score.review_cycle)

        ppi = save_ppi(
            score.employee,
            score.review_cycle,
            score.final_effective_score,
            past_scores
        )

        _invalidate_analytics(score.employee_questionnaire)

        return Response({
            "employee_id": score.employee_id,
            "review_cycle_id": score.review_cycle_id,
            "current_cycle_score": ppi.current_cycle_score,
            "previous_cycle_score_1": ppi.previous_cycle_score_1,
            "previous_cycle_score_2": ppi.previous_cycle_score_2,
            "ppi_score": ppi.ppi_score
        })


class PPIResultView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, questionnaire_id):
        if not is_scoring_admin(request.user):
            return Response({"error": "Not authorized."}, status=403)

        questionnaire = get_object_or_404(
            EmployeeQuestionnaire,
            id=questionnaire_id
        )

        ppi = get_object_or_404(
            EmployeePastPerformanceIndex,
            employee=questionnaire.employee,
            review_cycle=questionnaire.review_cycle
        )

        return Response({
            "employee_id": ppi.employee_id,
            "review_cycle_id": ppi.review_cycle_id,
            "current_cycle_score": ppi.current_cycle_score,
            "previous_cycle_score_1": ppi.previous_cycle_score_1,
            "previous_cycle_score_2": ppi.previous_cycle_score_2,
            "ppi_score": ppi.ppi_score
        })