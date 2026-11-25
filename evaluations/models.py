from django.db import models
from django.conf import settings
from django.core.exceptions import ValidationError
from django.db.models import Q

Employee = settings.AUTH_USER_MODEL


class QuestionCategory(models.Model):
    CATEGORY_CHOICES = [
        ("behavioral", "Behavioral"),
        ("performance", "Performance"),
    ]

    name = models.CharField(max_length=100)
    category_type = models.CharField(max_length=50, choices=CATEGORY_CHOICES)

    def __str__(self):
        return self.name


class QuestionParameter(models.Model):
    QUESTION_TYPES = [
        ("objective", "Objective"),
        ("descriptive", "Descriptive"),
    ]

    TARGET_MODES = [
        ("manual", "Question Bank Only (Assign Later)"),
        ("legacy", "Legacy Department Targeting"),
        ("all", "All Employees"),
        ("departments", "Selected Departments"),
        ("employees", "Selected Employees"),
        ("combined", "Departments and Employees"),
    ]

    category = models.ForeignKey(
        QuestionCategory,
        on_delete=models.CASCADE,
        related_name="parameters",
        null=True,
        blank=True
    )
    question_text = models.TextField()
    department = models.CharField(max_length=150, null=True, blank=True)
    default_weightage = models.FloatField(default=1)
    is_active = models.BooleanField(default=True)

    question_type = models.CharField(
        max_length=20,
        choices=QUESTION_TYPES,
        default="objective"
    )
    max_marks = models.PositiveIntegerField(default=10)
    target_mode = models.CharField(
        max_length=20,
        choices=TARGET_MODES,
        default="manual"
    )
    created_at = models.DateTimeField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    def clean(self):
        if self.default_weightage <= 0:
            raise ValidationError({
                "default_weightage": "Weightage must be greater than zero."
            })
        if self.max_marks < 1:
            raise ValidationError({
                "max_marks": "Maximum marks must be greater than zero."
            })
        if self.category_id is None and self.target_mode != "manual":
            raise ValidationError({
                "category": "Uncategorised questions must remain in Question Bank Only mode until a category is assigned."
            })

    def __str__(self):
        return self.question_text[:80]


class QuestionDepartmentAssignment(models.Model):
    parameter = models.ForeignKey(
        QuestionParameter,
        on_delete=models.CASCADE,
        related_name="department_assignments"
    )
    department = models.CharField(max_length=150)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["parameter", "department"],
                name="unique_question_department"
            )
        ]

    def __str__(self):
        return f"{self.parameter_id} - {self.department}"


class QuestionEmployeeAssignment(models.Model):
    parameter = models.ForeignKey(
        QuestionParameter,
        on_delete=models.CASCADE,
        related_name="employee_assignments"
    )
    employee = models.ForeignKey(
        Employee,
        on_delete=models.CASCADE,
        related_name="assigned_question_parameters"
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["parameter", "employee"],
                name="unique_question_employee"
            )
        ]


class QuestionEmployeeExclusion(models.Model):
    parameter = models.ForeignKey(
        QuestionParameter,
        on_delete=models.CASCADE,
        related_name="employee_exclusions"
    )
    employee = models.ForeignKey(
        Employee,
        on_delete=models.CASCADE,
        related_name="excluded_question_parameters"
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["parameter", "employee"],
                name="unique_question_exclusion"
            )
        ]


class ReviewCycle(models.Model):
    name = models.CharField(max_length=100)
    start_date = models.DateField()
    end_date = models.DateField()
    is_active = models.BooleanField(default=False)

    def __str__(self):
        return self.name


class EmployeeQuestionnaire(models.Model):
    STATUS_CHOICES = [
        ("draft", "Draft"),
        ("self_submitted", "Self Submitted"),
        ("under_rm_review", "Under RM Review"),
        ("rm_reviewed", "RM Reviewed"),
        ("under_skip_review", "Under Skip Review"),
        ("skip_reviewed", "Skip Reviewed"),
        ("under_peer_review", "Under Peer Review"),
        ("completed", "Completed"),
    ]

    employee = models.ForeignKey("employees.Employee", on_delete=models.CASCADE)
    review_cycle = models.ForeignKey("ReviewCycle", on_delete=models.CASCADE)
    status = models.CharField(max_length=50, choices=STATUS_CHOICES, default="draft")
    submitted_at = models.DateTimeField(null=True, blank=True)

    peer_reviewer = models.ForeignKey(
        "employees.Employee",
        null=True,
        blank=True,
        related_name="peer_reviews",
        on_delete=models.SET_NULL,
    )
    peer_requested_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    is_published = models.BooleanField(default=False)
    published_at = models.DateTimeField(null=True, blank=True)
    published_by = models.ForeignKey(
        Employee,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="published_questionnaires"
    )

    class Meta:
        unique_together = ("employee", "review_cycle")


class EmployeeQuestionnaireItem(models.Model):
    SOURCE_CHOICES = [
        ("automatic", "Automatic Assignment"),
        ("manual", "Admin Assignment"),
    ]

    employee_questionnaire = models.ForeignKey(
        EmployeeQuestionnaire,
        on_delete=models.CASCADE,
        related_name="items"
    )
    parameter = models.ForeignKey(
        QuestionParameter,
        on_delete=models.CASCADE
    )
    weightage = models.FloatField(default=1)
    order = models.IntegerField(default=0)

    source = models.CharField(
        max_length=20,
        choices=SOURCE_CHOICES,
        default="automatic"
    )
    question_text_snapshot = models.TextField(null=True, blank=True)
    question_type_snapshot = models.CharField(
        max_length=20,
        choices=QuestionParameter.QUESTION_TYPES,
        null=True,
        blank=True
    )
    max_marks_snapshot = models.PositiveIntegerField(null=True, blank=True)
    category_type_snapshot = models.CharField(max_length=50, null=True, blank=True)

    class Meta:
        ordering = ["order"]
        unique_together = ("employee_questionnaire", "parameter")

    def clean(self):
        if self.weightage <= 0:
            raise ValidationError({"weightage": "Weightage must be greater than zero."})


class EmployeeQuestionnaireCustomization(models.Model):
    employee_questionnaire = models.ForeignKey(
        EmployeeQuestionnaire,
        on_delete=models.CASCADE,
        related_name="customizations"
    )
    parameter = models.ForeignKey(
        QuestionParameter,
        on_delete=models.CASCADE
    )
    ACTION_CHOICES = [
        ("include", "Include"),
        ("exclude", "Exclude"),
    ]
    action = models.CharField(max_length=10, choices=ACTION_CHOICES)
    created_by = models.ForeignKey(
        Employee,
        on_delete=models.SET_NULL,
        null=True,
        blank=True
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["employee_questionnaire", "parameter"],
                name="unique_questionnaire_customization"
            )
        ]


class EmployeeQuestionnairePeerAssignment(models.Model):
    employee_questionnaire = models.ForeignKey(
        EmployeeQuestionnaire,
        on_delete=models.CASCADE,
        related_name="peer_assignments"
    )
    peer = models.ForeignKey(
        Employee,
        on_delete=models.CASCADE,
        related_name="assigned_peer_questionnaires"
    )
    assigned_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["employee_questionnaire", "peer"],
                name="unique_questionnaire_peer"
            )
        ]

    def clean(self):
        if self.employee_questionnaire_id and self.peer_id:
            if self.employee_questionnaire.employee_id == self.peer_id:
                raise ValidationError("Employees cannot review themselves.")


class EmployeeQuestionnaireStageSubmission(models.Model):
    STAGE_CHOICES = [
        ("self", "Self"),
        ("rm", "Reporting Manager"),
        ("skip", "Skip Level Manager"),
        ("peer", "Peer"),
    ]

    employee_questionnaire = models.ForeignKey(
        "EmployeeQuestionnaire",
        on_delete=models.CASCADE
    )
    evaluator_type = models.CharField(max_length=20, choices=STAGE_CHOICES)
    submitted_by = models.ForeignKey(
        "employees.Employee",
        on_delete=models.SET_NULL,
        null=True,
        blank=True
    )
    feedback = models.TextField(blank=True, null=True)
    scope_of_improvement = models.TextField(blank=True, null=True)
    status = models.CharField(max_length=20, default="draft")
    submitted_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["employee_questionnaire", "evaluator_type"],
                condition=~Q(evaluator_type="peer"),
                name="unique_nonpeer_stage"
            ),
            models.UniqueConstraint(
                fields=["employee_questionnaire", "submitted_by"],
                condition=Q(evaluator_type="peer"),
                name="unique_peer_stage_submitter"
            )
        ]


class EmployeeQuestionnaireResponse(models.Model):
    REVIEWER_TYPE_CHOICES = [
        ("self", "Self"),
        ("rm", "Reporting Manager"),
        ("skip", "Skip Manager"),
        ("peer", "Peer"),
    ]

    submission = models.ForeignKey(
        "EmployeeQuestionnaireStageSubmission",
        on_delete=models.CASCADE
    )
    questionnaire_item = models.ForeignKey(
        "EmployeeQuestionnaireItem",
        on_delete=models.CASCADE
    )
    score = models.IntegerField(null=True, blank=True)
    descriptive_answer = models.TextField(null=True, blank=True)
    is_not_applicable = models.BooleanField(default=False)
    reviewer_type = models.CharField(
        max_length=20,
        choices=REVIEWER_TYPE_CHOICES,
        default="self"
    )

    class Meta:
        unique_together = ("submission", "questionnaire_item", "reviewer_type")

    def clean(self):
        if self.is_not_applicable:
            if self.score is not None or self.descriptive_answer:
                raise ValidationError("N/A responses cannot contain a score or descriptive answer.")
            return

        if not self.questionnaire_item_id:
            return

        item = self.questionnaire_item
        question_type = item.question_type_snapshot or item.parameter.question_type

        if question_type == "descriptive":
            if self.score is not None:
                raise ValidationError({"score": "Descriptive responses cannot contain numerical scores."})
            if not (self.descriptive_answer or "").strip():
                raise ValidationError({"descriptive_answer": "A descriptive answer is required."})
        else:
            if self.score is None:
                raise ValidationError({"score": "An objective score is required."})
            max_marks = item.max_marks_snapshot or item.parameter.max_marks
            if not 0 <= self.score <= max_marks:
                raise ValidationError({"score": f"Score must be between 0 and {max_marks}."})
            if self.descriptive_answer:
                raise ValidationError({"descriptive_answer": "Objective responses cannot contain descriptive answers."})


class EmployeeDescriptiveScore(models.Model):
    response = models.OneToOneField(
        EmployeeQuestionnaireResponse,
        on_delete=models.CASCADE,
        related_name="admin_descriptive_score"
    )
    score = models.PositiveIntegerField()
    scored_by = models.ForeignKey(
        Employee,
        on_delete=models.SET_NULL,
        null=True,
        related_name="assigned_descriptive_scores"
    )
    remarks = models.TextField(blank=True, default="")
    scored_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def clean(self):
        if not self.response_id:
            raise ValidationError({"response": "A descriptive response is required."})

        response = self.response
        item = response.questionnaire_item
        question_type = item.question_type_snapshot or item.parameter.question_type

        if question_type != "descriptive":
            raise ValidationError({"response": "Only descriptive responses can receive admin scores."})

        if response.is_not_applicable:
            raise ValidationError({"response": "N/A responses cannot receive admin scores."})

        if not (response.descriptive_answer or "").strip():
            raise ValidationError({"response": "Cannot score an empty descriptive answer."})

        if response.submission.status != "submitted":
            raise ValidationError({"response": "Only submitted descriptive answers can be scored."})

        max_marks = item.max_marks_snapshot
        if max_marks is None:
            max_marks = item.parameter.max_marks

        if not 0 <= self.score <= max_marks:
            raise ValidationError({
                "score": f"Score must be between 0 and {max_marks}."
            })

    def save(self, *args, **kwargs):
        self.full_clean()
        super().save(*args, **kwargs)

    def __str__(self):
        return f"Response {self.response_id}: {self.score}"