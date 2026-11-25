from django.db import models
from django.db.models import Q
from django.conf import settings
from django.core.exceptions import ValidationError
from evaluations.models import EmployeeQuestionnaire

Employee = settings.AUTH_USER_MODEL


class ScoreConfiguration(models.Model):
    name = models.CharField(max_length=100, default="Default Configuration")
    behavioural_weight = models.FloatField(default=0.40)
    performance_weight = models.FloatField(default=0.60)
    self_weight = models.FloatField(default=0.10)
    rm_weight = models.FloatField(default=0.50)
    skip_weight = models.FloatField(default=0.25)
    peer_weight = models.FloatField(default=0.15)
    descriptive_scoring_enabled = models.BooleanField(default=False)
    objective_component_weight = models.FloatField(default=0.80)
    descriptive_component_weight = models.FloatField(default=0.20)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def clean(self):
        errors = {}
        weight_groups = {
            "category": [self.behavioural_weight, self.performance_weight],
            "evaluator": [self.self_weight, self.rm_weight, self.skip_weight, self.peer_weight],
            "component": [self.objective_component_weight, self.descriptive_component_weight],
        }
        for group, weights in weight_groups.items():
            if any(w is None or not 0 <= w <= 1 for w in weights):
                errors[group] = "All weights must be between 0 and 1."
            elif abs(sum(weights) - 1.0) > 0.000001:
                errors[group] = "Weights must add up to 1.0."
        if errors:
            raise ValidationError(errors)

    def __str__(self):
        return self.name


class EmployeePerformanceScore(models.Model):
    employee_questionnaire = models.OneToOneField(
        EmployeeQuestionnaire,
        on_delete=models.CASCADE
    )
    employee = models.ForeignKey(
        "employees.Employee",
        on_delete=models.CASCADE
    )
    review_cycle = models.ForeignKey(
        "evaluations.ReviewCycle",
        on_delete=models.CASCADE
    )

    # Category scores
    self_behavioural_score = models.FloatField(null=True, blank=True)
    self_performance_score = models.FloatField(null=True, blank=True)
    rm_behavioural_score = models.FloatField(null=True, blank=True)
    rm_performance_score = models.FloatField(null=True, blank=True)
    skip_behavioural_score = models.FloatField(null=True, blank=True)
    skip_performance_score = models.FloatField(null=True, blank=True)
    peer_behavioural_score = models.FloatField(null=True, blank=True)
    peer_performance_score = models.FloatField(null=True, blank=True)

    # Evaluator scores
    self_score = models.FloatField(null=True, blank=True)
    rm_score = models.FloatField(null=True, blank=True)
    skip_score = models.FloatField(null=True, blank=True)
    peer_score = models.FloatField(null=True, blank=True)

    # Effective evaluator weights
    effective_self_weight = models.FloatField(null=True, blank=True)
    effective_rm_weight = models.FloatField(null=True, blank=True)
    effective_skip_weight = models.FloatField(null=True, blank=True)
    effective_peer_weight = models.FloatField(null=True, blank=True)

    # Objective/descriptive components
    objective_final_score = models.FloatField(null=True, blank=True)
    descriptive_final_score = models.FloatField(null=True, blank=True)
    descriptive_scoring_applied = models.BooleanField(default=False)
    effective_objective_component_weight = models.FloatField(null=True, blank=True)
    effective_descriptive_component_weight = models.FloatField(null=True, blank=True)

    # Final score and HR override
    system_final_score = models.FloatField(null=True, blank=True)
    hr_override_score = models.FloatField(null=True, blank=True)
    final_effective_score = models.FloatField(null=True, blank=True)
    override_reason = models.TextField(null=True, blank=True)
    overridden_by = models.ForeignKey(
        "employees.Employee",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="score_overrides"
    )
    overridden_at = models.DateTimeField(null=True, blank=True)

    # Release control
    is_released_to_employee = models.BooleanField(default=False)
    released_by = models.ForeignKey(
        "employees.Employee",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="released_scores"
    )
    released_at = models.DateTimeField(null=True, blank=True)
    potential_score = models.FloatField(null=True, blank=True)
    nine_box_label = models.CharField(max_length=100, null=True, blank=True)
    calculated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"{self.employee} - Score"


class EmployeePastPerformanceIndex(models.Model):
    employee = models.ForeignKey(
        "employees.Employee",
        on_delete=models.CASCADE
    )
    review_cycle = models.ForeignKey(
        "evaluations.ReviewCycle",
        on_delete=models.CASCADE
    )
    current_cycle_score = models.FloatField()
    previous_cycle_score_1 = models.FloatField(null=True, blank=True)
    previous_cycle_score_2 = models.FloatField(null=True, blank=True)
    ppi_score = models.FloatField()
    calculated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = ("employee", "review_cycle")

    def __str__(self):
        return f"{self.employee} - PPI - {self.review_cycle}"


class PotentialParameter(models.Model):
    question_text = models.TextField()
    weightage = models.FloatField(default=1)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.question_text[:60]


class PotentialScoreConfiguration(models.Model):
    name = models.CharField(max_length=100, default="Default Potential Config")
    rm_weight = models.FloatField(default=0.6)
    skip_weight = models.FloatField(default=0.4)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.name


class EmployeePotentialAssessment(models.Model):
    employee = models.ForeignKey(
        "employees.Employee",
        on_delete=models.CASCADE
    )
    review_cycle = models.ForeignKey(
        "evaluations.ReviewCycle",
        on_delete=models.CASCADE
    )
    employee_questionnaire = models.OneToOneField(
        "evaluations.EmployeeQuestionnaire",
        on_delete=models.CASCADE
    )
    rm_submitted = models.BooleanField(default=False)
    skip_submitted = models.BooleanField(default=False)
    status = models.CharField(max_length=50, default="pending_rm")
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.employee} Potential Assessment"


class EmployeePotentialResponse(models.Model):
    assessment = models.ForeignKey(
        EmployeePotentialAssessment,
        on_delete=models.CASCADE
    )
    parameter = models.ForeignKey(
        PotentialParameter,
        on_delete=models.CASCADE
    )
    evaluator_type = models.CharField(
        max_length=10,
        choices=[
            ("rm", "RM"),
            ("skip", "Skip Manager"),
        ]
    )
    score = models.FloatField()
    submitted_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"{self.parameter} - {self.evaluator_type}"


class EmployeePotentialScore(models.Model):
    employee = models.ForeignKey(
        "employees.Employee",
        on_delete=models.CASCADE
    )
    review_cycle = models.ForeignKey(
        "evaluations.ReviewCycle",
        on_delete=models.CASCADE
    )
    employee_questionnaire = models.OneToOneField(
        "evaluations.EmployeeQuestionnaire",
        on_delete=models.CASCADE
    )
    rm_score = models.FloatField(null=True, blank=True)
    skip_score = models.FloatField(null=True, blank=True)
    final_potential_score = models.FloatField()
    calculated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"{self.employee} Potential Score"


class EmployeeNineBoxPlacement(models.Model):
    employee = models.ForeignKey(
        "employees.Employee",
        on_delete=models.CASCADE
    )
    review_cycle = models.ForeignKey(
        "evaluations.ReviewCycle",
        on_delete=models.CASCADE
    )
    employee_questionnaire = models.OneToOneField(
        "evaluations.EmployeeQuestionnaire",
        on_delete=models.CASCADE
    )
    ppi_score = models.FloatField()
    potential_score = models.FloatField()
    performance_bucket = models.CharField(max_length=20)
    potential_bucket = models.CharField(max_length=20)
    box_label = models.CharField(max_length=100)
    box_description = models.TextField()
    generated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"{self.employee} - {self.review_cycle} - {self.box_label}"


class AppraisalRecommendationConfig(models.Model):
    box_label = models.CharField(max_length=100, unique=True)
    min_percent = models.FloatField()
    max_percent = models.FloatField()
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"{self.box_label} ({self.min_percent}% - {self.max_percent}%)"


class EmployeeAppraisalRecommendation(models.Model):
    employee = models.ForeignKey(
        "employees.Employee",
        on_delete=models.CASCADE
    )
    review_cycle = models.ForeignKey(
        "evaluations.ReviewCycle",
        on_delete=models.CASCADE
    )
    employee_questionnaire = models.OneToOneField(
        "evaluations.EmployeeQuestionnaire",
        on_delete=models.CASCADE
    )
    nine_box_placement = models.ForeignKey(
        "scoring.EmployeeNineBoxPlacement",
        on_delete=models.CASCADE
    )
    ppi_score_used = models.FloatField()
    potential_score_used = models.FloatField()
    box_label = models.CharField(max_length=100)
    min_appraisal_percent = models.FloatField()
    max_appraisal_percent = models.FloatField()
    suggested_appraisal_percent = models.FloatField()
    hr_override_percent = models.FloatField(null=True, blank=True)
    override_reason = models.TextField(null=True, blank=True)
    overridden_by = models.ForeignKey(
        "employees.Employee",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="appraisal_overrides"
    )
    overridden_at = models.DateTimeField(null=True, blank=True)
    final_effective_appraisal_percent = models.FloatField()
    calculated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"{self.employee} - {self.review_cycle} - {self.final_effective_appraisal_percent}%"


class EmployeeAnalyticsRelease(models.Model):
    employee_questionnaire = models.OneToOneField(
        "evaluations.EmployeeQuestionnaire",
        on_delete=models.CASCADE,
        related_name="analytics_release",
    )
    is_released_to_employee = models.BooleanField(default=False)
    released_at = models.DateTimeField(null=True, blank=True)
    released_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="released_employee_analytics",
    )
    unreleased_at = models.DateTimeField(null=True, blank=True)
    unreleased_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="unreleased_employee_analytics",
    )
    show_performance_score = models.BooleanField(default=True)
    show_ppi = models.BooleanField(default=True)
    show_potential_score = models.BooleanField(default=True)
    show_nine_box = models.BooleanField(default=True)
    show_self_feedback = models.BooleanField(default=True)
    show_rm_feedback = models.BooleanField(default=True)
    show_skip_feedback = models.BooleanField(default=True)
    show_peer_feedback = models.BooleanField(default=True)
    show_self_scope_of_improvement = models.BooleanField(default=True)
    show_rm_scope_of_improvement = models.BooleanField(default=True)
    show_skip_scope_of_improvement = models.BooleanField(default=True)
    show_peer_scope_of_improvement = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "employee_analytics_release"

    def __str__(self):
        return f"Analytics Release - Questionnaire {self.employee_questionnaire_id}"


class EmployeeDescriptiveAssessment(models.Model):
    STATUS_CHOICES = [
        ("draft", "Draft"),
        ("finalized", "Finalized"),
    ]

    employee_questionnaire = models.OneToOneField(
        "evaluations.EmployeeQuestionnaire",
        on_delete=models.CASCADE,
        related_name="descriptive_assessment"
    )
    is_enabled = models.BooleanField(default=False)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="draft")
    descriptive_score = models.FloatField(null=True, blank=True)
    objective_weight_used = models.FloatField(null=True, blank=True)
    descriptive_weight_used = models.FloatField(null=True, blank=True)
    assessed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="descriptive_assessments"
    )
    assessed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"Descriptive Assessment - {self.employee_questionnaire_id}"


class EmployeeDescriptiveQuestionRating(models.Model):
    assessment = models.ForeignKey(
        EmployeeDescriptiveAssessment,
        on_delete=models.CASCADE,
        related_name="question_ratings"
    )
    questionnaire_item = models.ForeignKey(
        "evaluations.EmployeeQuestionnaireItem",
        on_delete=models.CASCADE
    )
    rating = models.PositiveSmallIntegerField(null=True, blank=True)
    weightage = models.FloatField(default=1)
    remarks = models.TextField(null=True, blank=True)
    rated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["assessment", "questionnaire_item"],
                name="unique_descriptive_question_rating"
            ),
            models.CheckConstraint(
                check=Q(rating__isnull=True) | (Q(rating__gte=1) & Q(rating__lte=10)),
                name="valid_descriptive_rating"
            ),
            models.CheckConstraint(
                check=Q(weightage__gt=0),
                name="positive_descriptive_weight"
            ),
    ]

    def __str__(self):
        return f"Assessment {self.assessment_id} - Item {self.questionnaire_item_id}"


class EmployeeDescriptiveAssessmentHistory(models.Model):
    assessment = models.ForeignKey(
        EmployeeDescriptiveAssessment,
        on_delete=models.CASCADE,
        related_name="history"
    )
    action = models.CharField(max_length=50)
    previous_status = models.CharField(max_length=20, null=True, blank=True)
    new_status = models.CharField(max_length=20, null=True, blank=True)
    previous_score = models.FloatField(null=True, blank=True)
    new_score = models.FloatField(null=True, blank=True)
    change_details = models.JSONField(default=dict, blank=True)
    changed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="descriptive_assessment_changes"
    )
    changed_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-changed_at", "-id"]

    def __str__(self):
        return f"Assessment {self.assessment_id} - {self.action}"