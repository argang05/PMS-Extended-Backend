from django.shortcuts import get_object_or_404
from django.db import transaction
from django.contrib.auth import get_user_model
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from evaluations.models import (
    EmployeeQuestionnaire, EmployeeQuestionnaireItem,
    EmployeeQuestionnaireStageSubmission, ReviewCycle
)
from evaluations.questionnaire_builder import (
    QuestionnaireBuilderError, EDIT_LOCK_MESSAGE, create_or_get_questionnaire,
    generate_questionnaire, include_question, exclude_question,
    reorder_questions, update_item_weightage,
    reset_questionnaire_customizations, copy_questionnaire,
    publish_questionnaire, questionnaire_details, _editable,
    _self_review_submitted, ensure_questionnaire_initialized
)

Employee = get_user_model()

def is_admin_user(user):
    return str(getattr(user, "employee_number", "")) == "100607"

class AdminBuilderBaseView(APIView):
    permission_classes = [IsAuthenticated]

    def initial(self, request, *args, **kwargs):
        super().initial(request, *args, **kwargs)
        if not is_admin_user(request.user):
            self.permission_denied(request, message="Not authorized.")

    def execute(self, operation):
        try:
            return operation()
        except QuestionnaireBuilderError as exc:
            return Response({"error": str(exc)}, status=400)

class AdminQuestionnaireListCreateView(AdminBuilderBaseView):
    def get(self, request):
        cycle_id = request.query_params.get("cycle_id")
        employee_number = request.query_params.get("employee_number", "").strip()
        status_filter = request.query_params.get("status", "").strip()
        published = request.query_params.get("is_published", "").strip()
        queryset = EmployeeQuestionnaire.objects.select_related(
            "employee", "review_cycle"
        ).order_by("-id")
        if cycle_id:
            queryset = queryset.filter(review_cycle_id=cycle_id)
        if employee_number:
            queryset = queryset.filter(employee__employee_number=employee_number)
        if status_filter:
            queryset = queryset.filter(status=status_filter)
        if published.lower() in ["true", "false"]:
            queryset = queryset.filter(is_published=published.lower() == "true")
        questionnaires = list(queryset)
        submitted_ids = set(EmployeeQuestionnaireStageSubmission.objects.filter(
            employee_questionnaire_id__in=[q.id for q in questionnaires],
            status="submitted"
        ).values_list("employee_questionnaire_id", flat=True))
        results = []
        for q in questionnaires:
            self_submitted = _self_review_submitted(q)
            editable = not self_submitted and q.id not in submitted_ids
            results.append({
                "questionnaire_id": q.id,
                "employee_id": q.employee_id,
                "employee_name": q.employee.full_name,
                "employee_number": q.employee.employee_number,
                "department": q.employee.department,
                "review_cycle_id": q.review_cycle_id,
                "review_cycle": q.review_cycle.name,
                "status": q.status,
                "is_published": q.is_published,
                "self_review_submitted": self_submitted,
                "is_editable": editable,
                "edit_lock_message": None if editable else EDIT_LOCK_MESSAGE,
                "question_count": q.items.count()
            })
        return Response({"results": results})

    def post(self, request):
        employee_number = str(request.data.get("employee_number") or "").strip()
        cycle_id = request.data.get("cycle_id")
        if not employee_number or not cycle_id:
            return Response({"error": "employee_number and cycle_id are required."}, status=400)
        employee = get_object_or_404(Employee, employee_number=employee_number)
        cycle = get_object_or_404(ReviewCycle, id=cycle_id)

        def operation():
            questionnaire, created = create_or_get_questionnaire(employee, cycle)
            return Response({
                "message": "Questionnaire created." if created else "Questionnaire already exists.",
                "created": created,
                "questionnaire": questionnaire_details(questionnaire)
            }, status=201 if created else 200)

        return self.execute(operation)

class AdminQuestionnaireDetailView(AdminBuilderBaseView):
    def get(self, request, questionnaire_id):
        def operation():
            questionnaire = ensure_questionnaire_initialized(questionnaire_id)
            return Response(questionnaire_details(questionnaire))
        return self.execute(operation)

class AdminQuestionnaireGenerateView(AdminBuilderBaseView):
    def post(self, request, questionnaire_id):
        return self.execute(lambda: Response({
            "message": "Questionnaire synchronized successfully.",
            **generate_questionnaire(questionnaire_id)
        }))

class AdminQuestionnaireIncludeView(AdminBuilderBaseView):
    def post(self, request, questionnaire_id):
        question_id = request.data.get("question_id")
        if question_id is None or question_id == "":
            return Response({"error": "question_id is required."}, status=400)

        def operation():
            with transaction.atomic():
                questionnaire = get_object_or_404(
                    EmployeeQuestionnaire.objects.select_for_update(), id=questionnaire_id
                )
                _editable(questionnaire)
                try:
                    parsed_id = int(question_id)
                except (TypeError, ValueError, OverflowError):
                    return Response({"error": "question_id must be a positive integer."}, status=400)
                if isinstance(question_id, bool) or parsed_id <= 0 or str(question_id).strip() != str(parsed_id):
                    return Response({"error": "question_id must be a positive integer."}, status=400)
                already_assigned = EmployeeQuestionnaireItem.objects.filter(
                    employee_questionnaire=questionnaire, parameter_id=parsed_id
                ).exists()
                if already_assigned:
                    return Response({
                        "error": f"Question #{parsed_id} is already assigned to this employee's questionnaire."
                    }, status=409)
                item = include_question(
                    questionnaire_id, parsed_id, request.user, request.data.get("weightage")
                )
                return Response({"message": "Question included successfully.", "item_id": item.id})

        return self.execute(operation)

class AdminQuestionnaireExcludeView(AdminBuilderBaseView):
    def post(self, request, questionnaire_id):
        question_id = request.data.get("question_id")
        if not question_id:
            return Response({"error": "question_id is required."}, status=400)
        return self.execute(lambda: Response({
            "message": "Question excluded successfully.",
            **exclude_question(questionnaire_id, question_id, request.user)
        }))

class AdminQuestionnaireReorderView(AdminBuilderBaseView):
    def post(self, request, questionnaire_id):
        return self.execute(lambda: Response({
            "message": "Question order updated successfully.",
            **reorder_questions(questionnaire_id, request.data.get("item_ids"))
        }))

class AdminQuestionnaireWeightageView(AdminBuilderBaseView):
    def put(self, request, questionnaire_id, item_id):
        if "weightage" not in request.data:
            return Response({"error": "weightage is required."}, status=400)

        def operation():
            item = update_item_weightage(
                questionnaire_id, item_id, request.data["weightage"]
            )
            return Response({
                "message": "Weightage updated successfully.",
                "item_id": item.id,
                "weightage": item.weightage
            })

        return self.execute(operation)

class AdminQuestionnaireResetView(AdminBuilderBaseView):
    def post(self, request, questionnaire_id):
        return self.execute(lambda: Response({
            "message": "Questionnaire customizations reset successfully.",
            **reset_questionnaire_customizations(questionnaire_id)
        }))

class AdminQuestionnaireCopyView(AdminBuilderBaseView):
    def post(self, request):
        source_id = request.data.get("source_questionnaire_id")
        destination_id = request.data.get("destination_questionnaire_id")
        if not source_id or not destination_id:
            return Response({
                "error": "source_questionnaire_id and destination_questionnaire_id are required."
            }, status=400)
        return self.execute(lambda: Response({
            "message": "Questionnaire copied successfully.",
            **copy_questionnaire(source_id, destination_id, request.user)
        }))

class AdminQuestionnairePublishView(AdminBuilderBaseView):
    def post(self, request, questionnaire_id):
        def operation():
            questionnaire = publish_questionnaire(questionnaire_id, request.user)
            return Response({
                "message": "Questionnaire published successfully.",
                "questionnaire": questionnaire_details(questionnaire)
            })
        return self.execute(operation)