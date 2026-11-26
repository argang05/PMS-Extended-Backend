from math import isclose, isfinite
from django.db import transaction
from django.utils import timezone
from django.shortcuts import get_object_or_404
from django.core.exceptions import ValidationError as DjangoValidationError
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from rest_framework.exceptions import ValidationError
from evaluations.models import EmployeeQuestionnaire
from .models import EmployeePerformanceScore, EmployeePastPerformanceIndex, EmployeePotentialAssessment
from .serializers import EmployeePerformanceScoreSerializer, HRScoreOverrideSerializer, ReleaseScoreSerializer
from .services import get_active_config, calculate_questionnaire_scores
from .services_ppi import get_past_scores, save_ppi_with_state, serialize_ppi, is_ppi_current
from .scoring_locks import lock_questionnaire
from .result_invalidation import invalidate_performance_dependencies, empty_invalidation_result

EVALUATORS = ("self", "rm", "skip", "peer")
CATEGORIES = ("behavioural", "performance")

def is_scoring_admin(user):
    return user.is_authenticated and user.is_active and str(getattr(user, "employee_number", "")) == "100607"

def _same(a, b, tolerance=1e-9):
    if a is None or b is None:
        return a is None and b is None
    try:
        return isclose(float(a), float(b), rel_tol=0, abs_tol=tolerance)
    except (TypeError, ValueError, OverflowError):
        return False

def _valid_score(value):
    if value is None or isinstance(value, bool):
        return False
    try:
        value = float(value)
        return isfinite(value) and 0 <= value <= 10
    except (TypeError, ValueError, OverflowError):
        return False

def _calculation(questionnaire):
    config = get_active_config()
    if not config:
        raise ValidationError({"error": "No active scoring configuration found."})
    try:
        return calculate_questionnaire_scores(questionnaire, config)
    except DjangoValidationError as exc:
        raise ValidationError({"error": exc.messages})

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

def _effective_evaluator_weights(result):
    weights = result["effective_weights"]
    objective_weight = result["effective_objective_component_weight"]
    descriptive_weight = result["effective_descriptive_component_weight"]
    if objective_weight > 0 and weights.get("objective"):
        return weights["objective"]
    if descriptive_weight > 0 and weights.get("descriptive"):
        return weights["descriptive"]
    return next(iter(weights.values()), {})

def _performance_values(questionnaire, result):
    values = {
        "employee_id": questionnaire.employee_id,
        "review_cycle_id": questionnaire.review_cycle_id,
        "objective_final_score": result["objective_final_score"],
        "descriptive_final_score": result["descriptive_final_score"],
        "descriptive_scoring_applied": result["descriptive_scoring_applied"],
        "effective_objective_component_weight": result["effective_objective_component_weight"],
        "effective_descriptive_component_weight": result["effective_descriptive_component_weight"],
        "system_final_score": result["system_final_score"]
    }
    effective_weights = _effective_evaluator_weights(result)
    for evaluator in EVALUATORS:
        for category in CATEGORIES:
            values[f"{evaluator}_{category}_score"] = _category_score(result, evaluator, category)
        values[f"{evaluator}_score"] = result["evaluator_scores"].get(evaluator)
        values[f"effective_{evaluator}_weight"] = effective_weights.get(evaluator, 0)
    return values

def _performance_values_changed(score, values):
    if score is None:
        return True
    for field, value in values.items():
        previous = getattr(score, field)
        if isinstance(value, bool) or field.endswith("_id"):
            if previous != value:
                return True
        elif not _same(previous, value):
            return True
    return False

def _current_score(questionnaire, score):
    if score is None or not _valid_score(score.system_final_score) or not _valid_score(score.final_effective_score):
        return False
    try:
        result = _calculation(questionnaire)
    except ValidationError:
        return False
    if not result["is_complete"]:
        return False
    values = _performance_values(questionnaire, result)
    if _performance_values_changed(score, values):
        return False
    if score.hr_override_score is not None and not _valid_score(score.hr_override_score):
        return False
    expected = score.hr_override_score if score.hr_override_score is not None else score.system_final_score
    return _same(score.final_effective_score, expected)

def _get_locked_score(questionnaire_id):
    questionnaire = lock_questionnaire(questionnaire_id)
    score = get_object_or_404(
        EmployeePerformanceScore.objects.select_for_update(),
        employee_questionnaire_id=questionnaire.pk
    )
    return questionnaire, score

def _serialize_score(score, **extra):
    return {**EmployeePerformanceScoreSerializer(score).data, **extra}

class AdminScoringQuestionnaireListView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        if not is_scoring_admin(request.user):
            return Response({"error": "Not authorized."}, status=403)
        questionnaires = list(EmployeeQuestionnaire.objects.select_related("employee", "review_cycle").order_by("-id"))
        questionnaire_ids = [questionnaire.pk for questionnaire in questionnaires]
        scores = {
            score.employee_questionnaire_id: score
            for score in EmployeePerformanceScore.objects.filter(employee_questionnaire_id__in=questionnaire_ids)
        }
        assessments = {
            assessment.employee_questionnaire_id: assessment
            for assessment in EmployeePotentialAssessment.objects.filter(employee_questionnaire_id__in=questionnaire_ids)
        }
        data = []
        for questionnaire in questionnaires:
            score = scores.get(questionnaire.pk)
            potential = assessments.get(questionnaire.pk)
            data.append({
                "questionnaire_id": questionnaire.pk,
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
                "assessment_id": potential.pk if potential else None,
                "potential_status": potential.status if potential else None,
                "potential_is_published": potential.is_published if potential else False
            })
        return Response(data)

class CalculateScoreView(APIView):
    permission_classes = [IsAuthenticated]

    @transaction.atomic
    def post(self, request, questionnaire_id):
        if not is_scoring_admin(request.user):
            return Response({"error": "Not authorized."}, status=403)
        questionnaire = lock_questionnaire(questionnaire_id)
        result = _calculation(questionnaire)
        if not result["is_complete"]:
            return Response({
                "error": "Performance scoring is incomplete.",
                "missing_requirements": result["missing_requirements"]
            }, status=400)
        existing = EmployeePerformanceScore.objects.select_for_update().filter(
            employee_questionnaire_id=questionnaire.pk
        ).first()
        values = _performance_values(questionnaire, result)
        override = existing.hr_override_score if existing else None
        if override is not None and not _valid_score(override):
            raise ValidationError("The existing HR override contains an invalid score.")
        values["final_effective_score"] = override if override is not None else result["system_final_score"]
        changed = _performance_values_changed(existing, values)
        invalidation = empty_invalidation_result()
        if changed:
            invalidation = invalidate_performance_dependencies(
                questionnaire, admin=request.user, preserve_score=True
            )
            score, _ = EmployeePerformanceScore.objects.update_or_create(
                employee_questionnaire_id=questionnaire.pk, defaults=values
            )
        else:
            score = existing
        return Response(_serialize_score(
            score, changed=changed, invalidation=invalidation,
            is_current=True, missing_requirements=[]
        ))

class ScoreResultView(APIView):
    permission_classes = [IsAuthenticated]

    @transaction.atomic
    def get(self, request, questionnaire_id):
        if not is_scoring_admin(request.user):
            return Response({"error": "Not authorized."}, status=403)
        questionnaire, score = _get_locked_score(questionnaire_id)
        current = _current_score(questionnaire, score)
        return Response(_serialize_score(
            score, is_current=current,
            missing_requirements=[] if current else ["Performance score requires recalculation."]
        ))

class HRScoreOverrideView(APIView):
    permission_classes = [IsAuthenticated]

    @transaction.atomic
    def put(self, request, questionnaire_id):
        if not is_scoring_admin(request.user):
            return Response({"error": "Not authorized."}, status=403)
        questionnaire, score = _get_locked_score(questionnaire_id)
        if not _current_score(questionnaire, score):
            return Response({
                "error": "Performance score is incomplete or outdated. Recalculate it first."
            }, status=409)
        serializer = HRScoreOverrideSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        override = serializer.validated_data["hr_override_score"]
        reason = serializer.validated_data["override_reason"].strip()
        if not _valid_score(override):
            return Response({"error": "Override score must be between 0 and 10."}, status=400)
        if not reason:
            return Response({"error": "Override reason is required."}, status=400)
        override = round(override, 2)
        changed = not _same(score.hr_override_score, override) or (score.override_reason or "") != reason
        effective_changed = not _same(score.final_effective_score, override)
        invalidation = empty_invalidation_result()
        if not changed:
            return Response(_serialize_score(score, changed=False, invalidation=invalidation))
        if effective_changed:
            invalidation = invalidate_performance_dependencies(
                questionnaire, admin=request.user, preserve_score=True
            )
            score.refresh_from_db()
        score.hr_override_score = override
        score.final_effective_score = override
        score.override_reason = reason
        score.overridden_by = request.user
        score.overridden_at = timezone.now()
        score.save(update_fields=[
            "hr_override_score", "final_effective_score", "override_reason",
            "overridden_by", "overridden_at"
        ])
        return Response(_serialize_score(score, changed=True, invalidation=invalidation))

class ReleaseScoreView(APIView):
    permission_classes = [IsAuthenticated]

    @transaction.atomic
    def put(self, request, questionnaire_id):
        if not is_scoring_admin(request.user):
            return Response({"error": "Not authorized."}, status=403)
        questionnaire, score = _get_locked_score(questionnaire_id)
        serializer = ReleaseScoreSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        release = serializer.validated_data["is_released_to_employee"]
        if release and not _current_score(questionnaire, score):
            return Response({
                "error": "Score cannot be released because its calculation is incomplete or outdated."
            }, status=409)
        changed = score.is_released_to_employee != release
        if changed:
            score.is_released_to_employee = release
            score.released_by = request.user if release else None
            score.released_at = timezone.now() if release else None
            score.save(update_fields=["is_released_to_employee", "released_by", "released_at"])
        return Response(_serialize_score(score, changed=changed))

class MyScoreView(APIView):
    permission_classes = [IsAuthenticated]

    @transaction.atomic
    def get(self, request, questionnaire_id=None):
        if not request.user.is_active:
            return Response({"error": "An active employee account is required."}, status=403)
        queryset = EmployeePerformanceScore.objects.filter(
            employee_id=request.user.pk, is_released_to_employee=True
        )
        if questionnaire_id is not None:
            queryset = queryset.filter(employee_questionnaire_id=questionnaire_id)
        selected_id = queryset.order_by("-review_cycle_id", "-id").values_list(
            "employee_questionnaire_id", flat=True
        ).first()
        if selected_id is None:
            return Response({"message": "Your score has not been released yet."}, status=403)
        questionnaire = lock_questionnaire(selected_id)
        score = EmployeePerformanceScore.objects.filter(
            employee_questionnaire_id=questionnaire.pk,
            employee_id=request.user.pk, is_released_to_employee=True
        ).first()
        if score is None or questionnaire.employee_id != request.user.pk:
            return Response({"message": "Your score has not been released yet."}, status=403)
        if not _current_score(questionnaire, score):
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
        questionnaire, score = _get_locked_score(questionnaire_id)
        if not _current_score(questionnaire, score):
            return Response({
                "error": "Recalculate the current performance score before calculating PPI."
            }, status=409)
        past_scores = get_past_scores(score.employee, score.review_cycle)
        ppi, changed, invalidation = save_ppi_with_state(
            score.employee, score.review_cycle, score.final_effective_score,
            past_scores, admin=request.user
        )
        return Response({
            **serialize_ppi(ppi), "changed": changed,
            "invalidation": invalidation, "is_current": True,
            "missing_requirements": []
        })

class PPIResultView(APIView):
    permission_classes = [IsAuthenticated]

    @transaction.atomic
    def get(self, request, questionnaire_id):
        if not is_scoring_admin(request.user):
            return Response({"error": "Not authorized."}, status=403)
        questionnaire = lock_questionnaire(questionnaire_id)
        ppi = get_object_or_404(
            EmployeePastPerformanceIndex,
            employee_id=questionnaire.employee_id,
            review_cycle_id=questionnaire.review_cycle_id
        )
        performance = EmployeePerformanceScore.objects.filter(
            employee_questionnaire_id=questionnaire.pk
        ).first()
        current = _current_score(questionnaire, performance) and is_ppi_current(
            questionnaire, performance=performance, ppi=ppi
        )
        return Response({
            **serialize_ppi(ppi), "is_current": current,
            "missing_requirements": [] if current else ["PPI or its Performance inputs require recalculation."]
        })