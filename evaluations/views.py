from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from rest_framework.exceptions import ValidationError
from django.utils import timezone
from django.db import transaction
from django.shortcuts import get_object_or_404
from django.contrib.auth import get_user_model
from django.db.models import Q
from .models import (
    ReviewCycle, EmployeeQuestionnaire, EmployeeQuestionnaireItem,
    EmployeeQuestionnaireCustomization, EmployeeQuestionnaireStageSubmission,
    EmployeeQuestionnaireResponse
)
from .questionnaire_builder import applicable_questions, _snapshot
from .peer_services import (
    PeerServiceError, add_peer, is_assigned_peer, peer_submission,
    peer_progress, all_peers_completed, refresh_questionnaire_completion
)

Employee = get_user_model()


def is_admin_user(user):
    return str(getattr(user, "employee_number", "")) == "100607"


def _question_text(item):
    return item.question_text_snapshot or item.parameter.question_text


def _question_type(item):
    return item.question_type_snapshot or item.parameter.question_type


def _max_marks(item):
    return item.max_marks_snapshot or item.parameter.max_marks


def _category_type(item):
    return item.category_type_snapshot or item.parameter.category.category_type


def _question_data(item, include_weightage=True):
    data = {
        "item_id": item.id,
        "question_text": _question_text(item),
        "category": item.parameter.category.name,
        "category_type": _category_type(item),
        "question_type": _question_type(item),
        "max_marks": _max_marks(item),
        "order": item.order,
    }
    if include_weightage:
        data["weightage"] = item.weightage
    return data


def _response_data(response):
    item = response.questionnaire_item
    return {
        "question": _question_text(item),
        "score": response.score,
        "descriptive_answer": response.descriptive_answer,
        "is_not_applicable": response.is_not_applicable,
        "question_type": _question_type(item),
        "item_id": item.id,
    }


def _positive_id(value, name):
    if isinstance(value, bool):
        raise ValidationError({"error": f"{name} must be a positive integer."})
    try:
        number = int(value)
    except (TypeError, ValueError, OverflowError):
        raise ValidationError({"error": f"{name} must be a positive integer."})
    if str(value).strip() != str(number) or number <= 0:
        raise ValidationError({"error": f"{name} must be a positive integer."})
    return number


def _validate_responses(questionnaire, responses, require_all=False, allow_na=False):
    if not isinstance(responses, list):
        raise ValidationError({"error": "responses must be a list."})

    items = {
        item.id: item
        for item in EmployeeQuestionnaireItem.objects.filter(
            employee_questionnaire=questionnaire
        ).select_related("parameter", "parameter__category")
    }

    if require_all and not items:
        raise ValidationError({"error": "Cannot submit a questionnaire without questions."})

    parsed = []
    seen = set()

    for index, response in enumerate(responses, 1):
        if not isinstance(response, dict):
            raise ValidationError({"error": f"Response {index} must be an object."})

        item_id = _positive_id(response.get("item_id"), "item_id")

        if item_id in seen:
            raise ValidationError({"error": f"Duplicate response for item {item_id}."})
        seen.add(item_id)

        item = items.get(item_id)
        if not item:
            raise ValidationError({"error": f"Invalid questionnaire item id: {item_id}."})

        question_type = _question_type(item)
        is_na = response.get("is_not_applicable", False)

        if not isinstance(is_na, bool):
            raise ValidationError({"error": f"is_not_applicable must be boolean for item {item_id}."})

        if is_na:
            if not allow_na:
                raise ValidationError({"error": "N/A is not supported for this evaluation stage."})
            if response.get("score") is not None or response.get("descriptive_answer"):
                raise ValidationError({"error": f"N/A item {item_id} cannot contain a score or answer."})
            parsed.append({
                "item": item, "score": None,
                "descriptive_answer": None, "is_not_applicable": True
            })
            continue

        if question_type == "descriptive":
            answer = response.get("descriptive_answer")
            if not isinstance(answer, str) or not answer.strip():
                raise ValidationError({"error": f"Descriptive answer is required for item {item_id}."})
            if response.get("score") is not None:
                raise ValidationError({"error": f"Descriptive item {item_id} cannot contain a numerical score."})
            parsed.append({
                "item": item, "score": None,
                "descriptive_answer": answer.strip(), "is_not_applicable": False
            })
            continue

        score = response.get("score")
        if isinstance(score, bool):
            raise ValidationError({"error": f"Invalid score for item {item_id}."})

        try:
            numeric_score = int(score)
        except (TypeError, ValueError, OverflowError):
            raise ValidationError({"error": f"Invalid score for item {item_id}."})

        if str(score).strip() != str(numeric_score):
            raise ValidationError({"error": f"Score must be an integer for item {item_id}."})

        if not 0 <= numeric_score <= _max_marks(item):
            raise ValidationError({
                "error": f"Score for item {item_id} must be between 0 and {_max_marks(item)}."
            })

        if response.get("descriptive_answer"):
            raise ValidationError({"error": f"Objective item {item_id} cannot contain a descriptive answer."})

        parsed.append({
            "item": item, "score": numeric_score,
            "descriptive_answer": None, "is_not_applicable": False
        })

    if require_all and seen != set(items):
        missing = sorted(set(items) - seen)
        raise ValidationError({
            "error": "All questionnaire questions must be answered.",
            "missing_item_ids": missing
        })

    return parsed


def _save_responses(submission, reviewer_type, parsed):
    existing = {
        response.questionnaire_item_id: response
        for response in EmployeeQuestionnaireResponse.objects.filter(
            submission=submission,
            reviewer_type=reviewer_type,
            questionnaire_item_id__in=[r["item"].id for r in parsed]
        )
    }

    to_create = []
    to_update = []

    for entry in parsed:
        item = entry["item"]
        response = existing.get(item.id)

        if response:
            response.score = entry["score"]
            response.descriptive_answer = entry["descriptive_answer"]
            response.is_not_applicable = entry["is_not_applicable"]
            to_update.append(response)
        else:
            to_create.append(EmployeeQuestionnaireResponse(
                submission=submission,
                questionnaire_item=item,
                reviewer_type=reviewer_type,
                score=entry["score"],
                descriptive_answer=entry["descriptive_answer"],
                is_not_applicable=entry["is_not_applicable"]
            ))

    if to_update:
        EmployeeQuestionnaireResponse.objects.bulk_update(
            to_update, ["score", "descriptive_answer", "is_not_applicable"]
        )
    if to_create:
        EmployeeQuestionnaireResponse.objects.bulk_create(to_create)


def _stage_submission(questionnaire, evaluator_type, user):
    if evaluator_type == "peer":
        try:
            return peer_submission(questionnaire, user, create=True)
        except PeerServiceError as exc:
            raise ValidationError({"error": str(exc)})

    submission, _ = EmployeeQuestionnaireStageSubmission.objects.get_or_create(
        employee_questionnaire=questionnaire,
        evaluator_type=evaluator_type,
        defaults={"submitted_by": user, "status": "draft"}
    )
    return submission


def _save_stage(questionnaire, evaluator_type, user, data, submit=False, allow_na=False):
    responses = data.get("responses", [])
    feedback = data.get("feedback")
    scope = data.get("scope_of_improvement")

    if submit and evaluator_type == "self":
        if not isinstance(feedback, str) or not feedback.strip():
            raise ValidationError({"error": "Feedback is required."})
        if not isinstance(scope, str) or not scope.strip():
            raise ValidationError({"error": "Scope of improvement is required."})

    parsed = _validate_responses(
        questionnaire, responses, require_all=submit, allow_na=allow_na
    )

    submission = _stage_submission(questionnaire, evaluator_type, user)

    if submission.status == "submitted":
        raise ValidationError({"error": "This evaluation has already been submitted."})

    _save_responses(submission, evaluator_type, parsed)

    submission.submitted_by = user
    submission.feedback = feedback
    submission.scope_of_improvement = scope
    submission.status = "submitted" if submit else "draft"
    submission.submitted_at = timezone.now() if submit else None
    submission.save()

    return submission


def _stage_responses(questionnaire, evaluator_type, submitted_only=False, reviewer=None):
    queryset = EmployeeQuestionnaireStageSubmission.objects.filter(
        employee_questionnaire=questionnaire,
        evaluator_type=evaluator_type
    )

    if evaluator_type == "peer":
        if reviewer is None:
            raise ValidationError({"error": "Peer reviewer is required."})
        queryset = queryset.filter(submitted_by=reviewer)

    if submitted_only:
        queryset = queryset.filter(status="submitted")

    submission = queryset.order_by("-submitted_at", "-id").first()

    if not submission:
        return {
            "feedback": "",
            "scope_of_improvement": "",
            "responses": []
        }

    responses = EmployeeQuestionnaireResponse.objects.filter(
        submission=submission,
        reviewer_type=evaluator_type
    ).select_related(
        "questionnaire_item__parameter__category"
    ).order_by("questionnaire_item__order", "id")

    return {
        "feedback": submission.feedback or "",
        "scope_of_improvement": submission.scope_of_improvement or "",
        "responses": [_response_data(r) for r in responses]
    }


def _get_questionnaire_for_update(questionnaire_id):
    """
    Lock only the EmployeeQuestionnaire row.

    PostgreSQL does not permit FOR UPDATE on the nullable side of
    an OUTER JOIN (such as the nullable peer_reviewer relation).

    Locking the questionnaire row also coordinates with admin
    questionnaire edits that acquire the same row lock.
    """
    return get_object_or_404(
        EmployeeQuestionnaire.objects
        .select_related("employee", "peer_reviewer", "review_cycle")
        .select_for_update(of=("self",)),
        id=questionnaire_id
    )


def _require_stage(questionnaire, stage):
    allowed = {
        "self": ["draft"],
        "rm": ["under_rm_review"],
        "skip": ["under_skip_review"],
        "peer": [
            "under_rm_review", "under_skip_review",
            "skip_reviewed", "under_peer_review"
        ],
    }

    if questionnaire.status not in allowed[stage]:
        raise ValidationError({
            "error": f"{stage.upper()} evaluation is not available at the current stage."
        })


def _is_skip_exempt(questionnaire):
    manager = questionnaire.employee.reporting_manager
    return str(getattr(manager, "band", "") or "").upper().startswith("SM")


def _peer_completed(questionnaire):
    return all_peers_completed(questionnaire)


def _has_peer(questionnaire):
    return peer_progress(questionnaire)["assigned"] > 0


def _generate_initial_items(questionnaire):
    questions = applicable_questions(questionnaire.employee)

    EmployeeQuestionnaireItem.objects.bulk_create([
        EmployeeQuestionnaireItem(
            employee_questionnaire=questionnaire,
            parameter=question,
            weightage=question.default_weightage,
            order=index,
            source="automatic",
            **_snapshot(question)
        )
        for index, question in enumerate(questions, 1)
    ])


class SelfQuestionnaireView(APIView):
    permission_classes = [IsAuthenticated]

    @transaction.atomic
    def get(self, request):
        employee = request.user
        cycle = ReviewCycle.objects.filter(is_active=True).order_by("id").first()

        if not cycle:
            return Response({"detail": "No active review cycle found."}, status=404)

        questionnaire, created = EmployeeQuestionnaire.objects.get_or_create(
            employee=employee,
            review_cycle=cycle,
            defaults={"status": "draft"}
        )

        questionnaire = EmployeeQuestionnaire.objects.select_for_update(
            of=("self",)
        ).get(id=questionnaire.id)

        if not questionnaire.items.exists():
            has_customizations = EmployeeQuestionnaireCustomization.objects.filter(
                employee_questionnaire=questionnaire
            ).exists()

            has_submissions = EmployeeQuestionnaireStageSubmission.objects.filter(
                employee_questionnaire=questionnaire
            ).exists()

            if (
                questionnaire.status == "draft"
                and not questionnaire.submitted_at
                and not has_customizations
                and not has_submissions
            ):
                _generate_initial_items(questionnaire)

        items = questionnaire.items.select_related(
            "parameter", "parameter__category"
        ).order_by("order", "id")

        questions = [{
            "id": item.id,
            "question_text": _question_text(item),
            "category": item.parameter.category.name,
            "category_type": _category_type(item),
            "weightage": item.weightage,
            "order": item.order,
            "question_type": _question_type(item),
            "max_marks": _max_marks(item)
        } for item in items]

        return Response({
            "questionnaire_id": questionnaire.id,
            "questions": questions,
            "is_published": questionnaire.is_published
        })


class SaveSelfEvaluation(APIView):
    permission_classes = [IsAuthenticated]

    @transaction.atomic
    def post(self, request):
        questionnaire_id = _positive_id(request.data.get("questionnaire_id"), "questionnaire_id")
        questionnaire = _get_questionnaire_for_update(questionnaire_id)

        if questionnaire.employee_id != request.user.id:
            return Response({"error": "Not authorized."}, status=403)

        _require_stage(questionnaire, "self")
        _save_stage(questionnaire, "self", request.user, request.data)
        return Response({"message": "Draft saved"})


class SubmitSelfEvaluation(APIView):
    permission_classes = [IsAuthenticated]

    @transaction.atomic
    def post(self, request):
        questionnaire_id = _positive_id(request.data.get("questionnaire_id"), "questionnaire_id")
        questionnaire = _get_questionnaire_for_update(questionnaire_id)

        if questionnaire.employee_id != request.user.id:
            return Response({"error": "Questionnaire not found or not authorized."}, status=404)

        _require_stage(questionnaire, "self")
        submission = _save_stage(
            questionnaire, "self", request.user, request.data, submit=True
        )

        questionnaire.status = "under_rm_review"
        questionnaire.submitted_at = submission.submitted_at
        questionnaire.save(update_fields=["status", "submitted_at"])

        return Response({"message": "Self evaluation submitted"})


class DeleteEmployeeQuestionnaireView(APIView):
    permission_classes = [IsAuthenticated]

    @transaction.atomic
    def delete(self, request, questionnaire_id):
        if not is_admin_user(request.user):
            return Response({"error": "Not authorized."}, status=403)

        questionnaire = get_object_or_404(
            EmployeeQuestionnaire.objects.select_for_update(of=("self",)),
            id=questionnaire_id
        )

        submissions = EmployeeQuestionnaireStageSubmission.objects.filter(
            employee_questionnaire=questionnaire
        )
        EmployeeQuestionnaireResponse.objects.filter(submission__in=submissions).delete()
        submissions.delete()
        EmployeeQuestionnaireItem.objects.filter(
            employee_questionnaire=questionnaire
        ).delete()
        questionnaire.delete()

        return Response({"message": "Questionnaire deleted successfully."})


class SelfReviewStatusView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        employee = request.user
        questionnaire = EmployeeQuestionnaire.objects.filter(
            employee=employee
        ).select_related("review_cycle", "peer_reviewer").order_by("-id").first()

        if not questionnaire:
            return Response({"exists": False})

        progress = peer_progress(questionnaire)

        return Response({
            "exists": True,
            "questionnaire_id": questionnaire.id,
            "employee_name": employee.full_name,
            "department": employee.department,
            "cycle": questionnaire.review_cycle.name if questionnaire.review_cycle else None,
            "status": questionnaire.status,
            "submitted_at": questionnaire.submitted_at,
            "peer_reviewer": questionnaire.peer_reviewer.full_name if questionnaire.peer_reviewer else None,
            "peer_completed": progress["all_completed"],
            "peer_review": progress,
            "is_published": questionnaire.is_published
        })


class RecommendPeerView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        questionnaire_id = _positive_id(request.data.get("questionnaire_id"), "questionnaire_id")
        peer_id = _positive_id(request.data.get("peer_id"), "peer_id")

        try:
            result = add_peer(questionnaire_id, peer_id, request.user)
        except EmployeeQuestionnaire.DoesNotExist:
            return Response({"error": "Questionnaire not found."}, status=404)
        except PeerServiceError as exc:
            return Response({"error": str(exc)}, status=400)

        return Response({
            "message": "Peer recommended successfully",
            "peer_review": result["peer_review"]
        })


class ReportingManagerQueueView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        questionnaires = EmployeeQuestionnaire.objects.filter(
            employee__reporting_manager=request.user
        ).select_related("employee", "review_cycle").order_by("-id")

        return Response([{
            "questionnaire_id": q.id,
            "employee_name": q.employee.full_name,
            "employee_number": q.employee.employee_number,
            "department": q.employee.department,
            "submitted_at": q.submitted_at,
            "cycle": q.review_cycle.name,
            "status": q.status
        } for q in questionnaires])


class RMReviewFormView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, questionnaire_id):
        questionnaire = get_object_or_404(
            EmployeeQuestionnaire.objects.select_related("employee"),
            id=questionnaire_id
        )

        if questionnaire.employee.reporting_manager_id != request.user.id:
            return Response({"error": "Not authorized to review this questionnaire"}, status=403)

        items = EmployeeQuestionnaireItem.objects.filter(
            employee_questionnaire=questionnaire
        ).select_related("parameter", "parameter__category").order_by("order", "id")

        return Response({
            "questionnaire_id": questionnaire.id,
            "employee_name": questionnaire.employee.full_name,
            "department": questionnaire.employee.department,
            "questions": [_question_data(item) for item in items]
        })


class RMViewSelfResponses(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, questionnaire_id):
        questionnaire = get_object_or_404(
            EmployeeQuestionnaire.objects.select_related("employee"),
            id=questionnaire_id
        )

        if questionnaire.employee.reporting_manager_id != request.user.id:
            return Response({"error": "Not authorized"}, status=403)

        return Response(_stage_responses(questionnaire, "self"))


class RMSaveDraftView(APIView):
    permission_classes = [IsAuthenticated]

    @transaction.atomic
    def post(self, request):
        questionnaire_id = _positive_id(request.data.get("questionnaire_id"), "questionnaire_id")
        questionnaire = _get_questionnaire_for_update(questionnaire_id)

        if questionnaire.employee.reporting_manager_id != request.user.id:
            return Response({"error": "Not authorized."}, status=403)

        _require_stage(questionnaire, "rm")
        _save_stage(questionnaire, "rm", request.user, request.data)
        return Response({"message": "Draft saved"})


class RMSubmitReviewView(APIView):
    permission_classes = [IsAuthenticated]

    @transaction.atomic
    def post(self, request):
        questionnaire_id = _positive_id(request.data.get("questionnaire_id"), "questionnaire_id")
        questionnaire = _get_questionnaire_for_update(questionnaire_id)

        if questionnaire.employee.reporting_manager_id != request.user.id:
            return Response({"error": "Not authorized to review this questionnaire."}, status=403)

        _require_stage(questionnaire, "rm")
        _save_stage(questionnaire, "rm", request.user, request.data, submit=True)

        if _is_skip_exempt(questionnaire):
            refresh_questionnaire_completion(questionnaire)
        else:
            questionnaire.status = "under_skip_review"
            questionnaire.save(update_fields=["status"])

        return Response({"message": "RM Review Submitted"})


class SkipPendingReviewsView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        questionnaires = EmployeeQuestionnaire.objects.filter(
            employee__skip_level_manager=request.user,
            status__in=["under_skip_review", "completed", "skip_reviewed", "under_peer_review"]
        ).select_related("employee")

        return Response({"results": [{
            "questionnaire_id": q.id,
            "employee_name": q.employee.full_name,
            "employee_number": q.employee.employee_number,
            "department": q.employee.department or None,
            "status": q.status,
            "submitted_at": q.submitted_at
        } for q in questionnaires]})


class SkipReviewFormView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, questionnaire_id):
        questionnaire = get_object_or_404(
            EmployeeQuestionnaire.objects.select_related("employee"),
            id=questionnaire_id
        )

        if questionnaire.employee.skip_level_manager_id != request.user.id:
            return Response({"error": "Not authorized for this questionnaire"}, status=403)

        items = EmployeeQuestionnaireItem.objects.filter(
            employee_questionnaire=questionnaire
        ).select_related("parameter", "parameter__category").order_by("order", "id")

        return Response({
            "employee_name": questionnaire.employee.full_name,
            "department": questionnaire.employee.department or None,
            "questions": [_question_data(item) for item in items]
        })


class SkipSelfResponsesView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, questionnaire_id):
        questionnaire = get_object_or_404(
            EmployeeQuestionnaire.objects.select_related("employee"),
            id=questionnaire_id
        )

        if questionnaire.employee.skip_level_manager_id != request.user.id:
            return Response({"error": "Not authorized for this questionnaire"}, status=403)

        return Response(_stage_responses(questionnaire, "self"))


class SkipRMResponsesView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, questionnaire_id):
        questionnaire = get_object_or_404(
            EmployeeQuestionnaire.objects.select_related("employee"),
            id=questionnaire_id
        )

        if questionnaire.employee.skip_level_manager_id != request.user.id:
            return Response({"error": "Not authorized for this questionnaire"}, status=403)

        return Response(_stage_responses(questionnaire, "rm", submitted_only=True))


class SkipSaveDraftView(APIView):
    permission_classes = [IsAuthenticated]

    @transaction.atomic
    def post(self, request):
        questionnaire_id = _positive_id(request.data.get("questionnaire_id"), "questionnaire_id")
        questionnaire = _get_questionnaire_for_update(questionnaire_id)

        if questionnaire.employee.skip_level_manager_id != request.user.id:
            return Response({"error": "Not authorized for this questionnaire"}, status=403)

        _require_stage(questionnaire, "skip")
        _save_stage(questionnaire, "skip", request.user, request.data)
        return Response({"message": "Skip draft saved successfully"})


class SkipSubmitReviewView(APIView):
    permission_classes = [IsAuthenticated]

    @transaction.atomic
    def post(self, request):
        questionnaire_id = _positive_id(request.data.get("questionnaire_id"), "questionnaire_id")
        questionnaire = _get_questionnaire_for_update(questionnaire_id)

        if questionnaire.employee.skip_level_manager_id != request.user.id:
            return Response({"error": "Not authorized for this questionnaire"}, status=403)

        _require_stage(questionnaire, "skip")
        _save_stage(questionnaire, "skip", request.user, request.data, submit=True)
        refresh_questionnaire_completion(questionnaire)

        return Response({"message": "Skip review submitted successfully"})


class PeerEmployeeSearchView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        query = request.query_params.get("q", "").strip()
        employees = Employee.objects.filter(is_active=True).exclude(id=request.user.id)

        if query:
            employees = employees.filter(
                Q(first_name__icontains=query) |
                Q(last_name__icontains=query) |
                Q(employee_number__icontains=query)
            )

        employees = employees.order_by("first_name", "last_name")[:25]

        return Response([{
            "id": employee.id,
            "employee_number": employee.employee_number,
            "full_name": employee.full_name,
            "department": employee.department
        } for employee in employees])


class PeerRecommendView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        questionnaire_id = _positive_id(request.data.get("questionnaire_id"), "questionnaire_id")
        peer_id = _positive_id(request.data.get("peer_employee_id"), "peer_employee_id")

        try:
            result = add_peer(questionnaire_id, peer_id, request.user)
        except EmployeeQuestionnaire.DoesNotExist:
            return Response({"error": "Questionnaire not found."}, status=404)
        except PeerServiceError as exc:
            return Response({"error": str(exc)}, status=400)

        return Response({
            "message": "Peer review request sent successfully",
            "peer_review": result["peer_review"]
        })


class PeerPendingReviewsView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        peer = request.user

        questionnaires = EmployeeQuestionnaire.objects.filter(
            Q(peer_assignments__peer=peer) | Q(peer_reviewer=peer)
        ).exclude(status="draft").select_related(
            "employee", "review_cycle"
        ).distinct().order_by("-id")

        data = []

        for questionnaire in questionnaires:
            submission = EmployeeQuestionnaireStageSubmission.objects.filter(
                employee_questionnaire=questionnaire,
                evaluator_type="peer",
                submitted_by=peer
            ).first()

            data.append({
                "questionnaire_id": questionnaire.id,
                "employee_name": questionnaire.employee.full_name,
                "employee_number": questionnaire.employee.employee_number,
                "department": questionnaire.employee.department,
                "cycle": questionnaire.review_cycle.name if questionnaire.review_cycle else None,
                "submitted_at": questionnaire.submitted_at,
                "peer_completed": bool(submission and submission.status == "submitted"),
                "peer_status": submission.status if submission else "pending",
                "status": questionnaire.status,
                "peer_requested_at": questionnaire.peer_requested_at
            })

        return Response({"results": data})


class PeerReviewFormView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, questionnaire_id):
        questionnaire = get_object_or_404(
            EmployeeQuestionnaire.objects.select_related("employee"),
            id=questionnaire_id
        )

        if not is_assigned_peer(questionnaire, request.user):
            return Response({"error": "Not authorized for this questionnaire"}, status=403)

        items = EmployeeQuestionnaireItem.objects.filter(
            employee_questionnaire=questionnaire
        ).select_related("parameter", "parameter__category").order_by("order", "id")

        submission = peer_submission(questionnaire, request.user)

        return Response({
            "employee_name": questionnaire.employee.full_name,
            "department": questionnaire.employee.department,
            "questions": [_question_data(item) for item in items],
            "peer_status": submission.status if submission else "pending"
        })


class PeerSaveDraftView(APIView):
    permission_classes = [IsAuthenticated]

    @transaction.atomic
    def post(self, request):
        questionnaire_id = _positive_id(request.data.get("questionnaire_id"), "questionnaire_id")
        questionnaire = _get_questionnaire_for_update(questionnaire_id)

        if not is_assigned_peer(questionnaire, request.user):
            return Response({"error": "Not authorized"}, status=403)

        _require_stage(questionnaire, "peer")
        _save_stage(questionnaire, "peer", request.user, request.data, allow_na=True)

        return Response({"message": "Draft saved"})


class PeerSubmitReviewView(APIView):
    permission_classes = [IsAuthenticated]

    @transaction.atomic
    def post(self, request):
        questionnaire_id = _positive_id(request.data.get("questionnaire_id"), "questionnaire_id")
        questionnaire = _get_questionnaire_for_update(questionnaire_id)

        if not is_assigned_peer(questionnaire, request.user):
            return Response({"error": "Not authorized"}, status=403)

        _require_stage(questionnaire, "peer")
        _save_stage(
            questionnaire, "peer", request.user,
            request.data, submit=True, allow_na=True
        )

        refresh_questionnaire_completion(questionnaire)
        progress = peer_progress(questionnaire)

        return Response({
            "message": "Peer review submitted successfully",
            "peer_review": progress,
            "questionnaire_status": questionnaire.status
        })


class PeerSelfResponsesView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, questionnaire_id):
        questionnaire = get_object_or_404(
            EmployeeQuestionnaire.objects.select_related("employee"),
            id=questionnaire_id
        )

        if not is_assigned_peer(questionnaire, request.user):
            return Response({"error": "Not authorized for this questionnaire"}, status=403)

        return Response(_stage_responses(questionnaire, "self"))