from django.db import transaction
from django.shortcuts import get_object_or_404
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from .models import (
    EmployeeNineBoxPlacement, EmployeePerformanceScore,
    EmployeePastPerformanceIndex, EmployeePotentialScore
)
from .services_ninebox import save_nine_box_placement, build_nine_box_result
from .scoring_locks import lock_questionnaire
from .score_readiness import (
    get_ppi_readiness, get_potential_readiness,
    get_ninebox_readiness, same_number
)
from .result_invalidation import invalidate_ninebox_dependencies

def is_scoring_admin(user):
    return user.is_authenticated and user.is_active and str(getattr(user, "employee_number", "")) == "100607"

def serialize_placement(questionnaire, placement):
    return {
        "questionnaire_id": questionnaire.pk,
        "employee_name": questionnaire.employee.full_name,
        "employee_number": questionnaire.employee.employee_number,
        "review_cycle": questionnaire.review_cycle.name if questionnaire.review_cycle else None,
        "ppi_score": placement.ppi_score,
        "potential_score": placement.potential_score,
        "performance_bucket": placement.performance_bucket,
        "potential_bucket": placement.potential_bucket,
        "box_label": placement.box_label,
        "box_description": placement.box_description,
        "generated_at": placement.generated_at
    }

def placement_changed(existing, expected):
    if existing is None:
        return True
    for field in ("ppi_score", "potential_score"):
        if not same_number(getattr(existing, field), expected[field], 1e-9):
            return True
    for field in ("performance_bucket", "potential_bucket", "box_label", "box_description"):
        if getattr(existing, field) != expected[field]:
            return True
    return False

class GenerateNineBoxPlacementView(APIView):
    permission_classes = [IsAuthenticated]

    @transaction.atomic
    def post(self, request, questionnaire_id):
        if not is_scoring_admin(request.user):
            return Response({"error": "You are not authorized to generate 9-box placement."}, status=403)
        questionnaire = lock_questionnaire(questionnaire_id)
        ppi_record = EmployeePastPerformanceIndex.objects.filter(
            employee_id=questionnaire.employee_id,
            review_cycle_id=questionnaire.review_cycle_id
        ).first()
        potential_record = EmployeePotentialScore.objects.filter(
            employee_questionnaire_id=questionnaire.pk
        ).first()
        ppi_state = get_ppi_readiness(questionnaire, ppi=ppi_record)
        potential_state = get_potential_readiness(questionnaire, score=potential_record)
        missing = list(dict.fromkeys(
            ppi_state["missing_requirements"] + potential_state["missing_requirements"]
        ))
        if not ppi_state["is_current"] or not potential_state["is_current"] or ppi_record is None or potential_record is None:
            return Response({
                "error": "9-box placement requires current PPI and Potential scores.",
                "missing_requirements": missing
            }, status=409)
        ppi_score = ppi_record.ppi_score
        potential_score = potential_record.final_potential_score
        expected = build_nine_box_result(ppi_score, potential_score)
        existing = EmployeeNineBoxPlacement.objects.select_for_update().filter(
            employee_questionnaire_id=questionnaire.pk
        ).first()
        changed = placement_changed(existing, expected)
        invalidation = {"invalidated": False, "affected_results": []}
        if changed:
            invalidation = invalidate_ninebox_dependencies(questionnaire, request.user)
            placement = save_nine_box_placement(
                employee=questionnaire.employee,
                review_cycle=questionnaire.review_cycle,
                employee_questionnaire=questionnaire,
                ppi_score=ppi_score,
                potential_score=potential_score
            )
        else:
            placement = existing
        EmployeePerformanceScore.objects.filter(
            employee_questionnaire_id=questionnaire.pk
        ).exclude(
            potential_score=placement.potential_score,
            nine_box_label=placement.box_label
        ).update(
            potential_score=placement.potential_score,
            nine_box_label=placement.box_label
        )
        return Response({
            "message": "9-box placement generated successfully.",
            **serialize_placement(questionnaire, placement),
            "changed": changed,
            "invalidation": invalidation
        })

class NineBoxPlacementResultView(APIView):
    permission_classes = [IsAuthenticated]

    @transaction.atomic
    def get(self, request, questionnaire_id):
        if not is_scoring_admin(request.user):
            return Response({"error": "You are not authorized to view 9-box placement."}, status=403)
        questionnaire = lock_questionnaire(questionnaire_id)
        placement = get_object_or_404(
            EmployeeNineBoxPlacement, employee_questionnaire_id=questionnaire.pk
        )
        readiness = get_ninebox_readiness(questionnaire, placement=placement)
        return Response({
            **serialize_placement(questionnaire, placement),
            "is_current": readiness["is_ready"],
            "missing_requirements": readiness["missing_requirements"]
        })