from django.db import models
from django.db.models import Q
from django.conf import settings
from django.core.exceptions import ValidationError
from evaluations.models import EmployeeQuestionnaire
from math import isfinite

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
    employee_questionnaire = models.OneToOneField(EmployeeQuestionnaire, on_delete=models.CASCADE)
    employee = models.ForeignKey("employees.Employee", on_delete=models.CASCADE)
    review_cycle = models.ForeignKey("evaluations.ReviewCycle", on_delete=models.CASCADE)
    self_behavioural_score = models.FloatField(null=True, blank=True)
    self_performance_score = models.FloatField(null=True, blank=True)
    rm_behavioural_score = models.FloatField(null=True, blank=True)
    rm_performance_score = models.FloatField(null=True, blank=True)
    skip_behavioural_score = models.FloatField(null=True, blank=True)
    skip_performance_score = models.FloatField(null=True, blank=True)
    peer_behavioural_score = models.FloatField(null=True, blank=True)
    peer_performance_score = models.FloatField(null=True, blank=True)
    self_score = models.FloatField(null=True, blank=True)
    rm_score = models.FloatField(null=True, blank=True)
    skip_score = models.FloatField(null=True, blank=True)
    peer_score = models.FloatField(null=True, blank=True)
    effective_self_weight = models.FloatField(null=True, blank=True)
    effective_rm_weight = models.FloatField(null=True, blank=True)
    effective_skip_weight = models.FloatField(null=True, blank=True)
    effective_peer_weight = models.FloatField(null=True, blank=True)
    objective_final_score = models.FloatField(null=True, blank=True)
    descriptive_final_score = models.FloatField(null=True, blank=True)
    descriptive_scoring_applied = models.BooleanField(default=False)
    effective_objective_component_weight = models.FloatField(null=True, blank=True)
    effective_descriptive_component_weight = models.FloatField(null=True, blank=True)
    system_final_score = models.FloatField(null=True, blank=True)
    hr_override_score = models.FloatField(null=True, blank=True)
    final_effective_score = models.FloatField(null=True, blank=True)
    override_reason = models.TextField(null=True, blank=True)
    overridden_by = models.ForeignKey("employees.Employee", null=True, blank=True, on_delete=models.SET_NULL, related_name="score_overrides")
    overridden_at = models.DateTimeField(null=True, blank=True)
    is_released_to_employee = models.BooleanField(default=False)
    released_by = models.ForeignKey("employees.Employee", null=True, blank=True, on_delete=models.SET_NULL, related_name="released_scores")
    released_at = models.DateTimeField(null=True, blank=True)
    potential_score = models.FloatField(null=True, blank=True)
    nine_box_label = models.CharField(max_length=100, null=True, blank=True)
    calculated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"{self.employee} - Score"

class EmployeePastPerformanceIndex(models.Model):
    employee = models.ForeignKey("employees.Employee", on_delete=models.CASCADE)
    review_cycle = models.ForeignKey("evaluations.ReviewCycle", on_delete=models.CASCADE)
    current_cycle_score = models.FloatField()
    previous_cycle_score_1 = models.FloatField(null=True, blank=True)
    previous_cycle_score_2 = models.FloatField(null=True, blank=True)
    ppi_score = models.FloatField()
    calculated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = ("employee", "review_cycle")

    def __str__(self):
        return f"{self.employee} - PPI - {self.review_cycle}"

class PotentialQuestionCategory(models.Model):
    name = models.CharField(max_length=100, unique=True)

    def __str__(self):
        return self.name

class PotentialParameter(models.Model):
    QUESTION_TYPES = [
        ("objective", "Objective"),
        ("descriptive", "Descriptive"),
    ]
    TARGET_MODES = [
        ("manual", "Question Bank Only (Assign Later)"),
        ("legacy", "Legacy Assignment"),
        ("all", "All Employees"),
        ("departments", "Selected Departments"),
        ("employees", "Selected Employees"),
        ("combined", "Departments and Employees"),
    ]
    category = models.ForeignKey(PotentialQuestionCategory, on_delete=models.SET_NULL, related_name="parameters", null=True, blank=True)
    question_text = models.TextField()
    weightage = models.FloatField(default=1)
    is_active = models.BooleanField(default=True)
    question_type = models.CharField(max_length=20, choices=QUESTION_TYPES, default="objective")
    max_marks = models.PositiveIntegerField(default=10)
    target_mode = models.CharField(max_length=20, choices=TARGET_MODES, default="manual")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def clean(self):
        errors = {}
        if not (self.question_text or "").strip():
            errors["question_text"] = "Question text is required."
        if self.weightage is None or not isfinite(self.weightage) or self.weightage <= 0:
            errors["weightage"] = "Weightage must be greater than zero."
        if self.max_marks is None or self.max_marks < 1:
            errors["max_marks"] = "Maximum marks must be greater than zero."
        if errors:
            raise ValidationError(errors)

    def __str__(self):
        return self.question_text[:60]

class PotentialDepartmentAssignment(models.Model):
    parameter = models.ForeignKey(PotentialParameter, on_delete=models.CASCADE, related_name="department_assignments")
    department = models.CharField(max_length=150)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["parameter", "department"], name="unique_potential_question_department")
        ]

    def __str__(self):
        return f"{self.parameter_id} - {self.department}"

class PotentialEmployeeAssignment(models.Model):
    parameter = models.ForeignKey(PotentialParameter, on_delete=models.CASCADE, related_name="employee_assignments")
    employee = models.ForeignKey(Employee, on_delete=models.CASCADE, related_name="assigned_potential_parameters")

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["parameter", "employee"], name="unique_potential_question_employee")
        ]

    def __str__(self):
        return f"{self.parameter_id} - {self.employee_id}"

class PotentialEmployeeExclusion(models.Model):
    parameter = models.ForeignKey(PotentialParameter, on_delete=models.CASCADE, related_name="employee_exclusions")
    employee = models.ForeignKey(Employee, on_delete=models.CASCADE, related_name="excluded_potential_parameters")

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["parameter", "employee"], name="unique_potential_question_exclusion")
        ]

    def __str__(self):
        return f"{self.parameter_id} - {self.employee_id}"

class PotentialScoreConfiguration(models.Model):
    name = models.CharField(max_length=100, default="Default Potential Config")
    rm_weight = models.FloatField(default=0.6)
    skip_weight = models.FloatField(default=0.4)
    objective_component_weight = models.FloatField(default=0.80)
    descriptive_component_weight = models.FloatField(default=0.20)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def clean(self):
        errors = {}
        weight_groups = {
            "evaluator": [self.rm_weight, self.skip_weight],
            "component": [self.objective_component_weight, self.descriptive_component_weight],
        }
        for group, weights in weight_groups.items():
            if any(w is None or not isfinite(w) or not 0 <= w <= 1 for w in weights):
                errors[group] = "All weights must be between 0 and 1."
            elif abs(sum(weights) - 1.0) > 0.000001:
                errors[group] = "Weights must add up to 1.0."
        if errors:
            raise ValidationError(errors)

    def __str__(self):
        return self.name

class EmployeePotentialAssessment(models.Model):
    STATUS_CHOICES = [
        ("draft", "Draft"),
        ("pending_rm", "Pending RM Review"),
        ("pending_skip", "Pending Skip Review"),
        ("completed", "Completed"),
    ]
    employee = models.ForeignKey("employees.Employee", on_delete=models.CASCADE)
    review_cycle = models.ForeignKey("evaluations.ReviewCycle", on_delete=models.CASCADE)
    employee_questionnaire = models.OneToOneField("evaluations.EmployeeQuestionnaire", on_delete=models.CASCADE)
    rm_submitted = models.BooleanField(default=False)
    skip_submitted = models.BooleanField(default=False)
    status = models.CharField(max_length=50, default="pending_rm")
    created_at = models.DateTimeField(auto_now_add=True)
    is_published = models.BooleanField(default=False)
    published_at = models.DateTimeField(null=True, blank=True)
    published_by = models.ForeignKey(Employee, on_delete=models.SET_NULL, null=True, blank=True, related_name="published_potential_assessments")
    customization_draft = models.JSONField(null=True, blank=True, default=None)
    reporting_manager = models.ForeignKey(Employee, on_delete=models.SET_NULL, null=True, blank=True, related_name="assigned_rm_potential_assessments")
    skip_level_manager = models.ForeignKey(Employee, on_delete=models.SET_NULL, null=True, blank=True, related_name="assigned_skip_potential_assessments")
    rm_submitted_at = models.DateTimeField(null=True, blank=True)
    skip_submitted_at = models.DateTimeField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    def clean(self):
        errors = {}
        if self.employee_questionnaire_id:
            questionnaire = self.employee_questionnaire
            if self.employee_id != questionnaire.employee_id:
                errors["employee"] = "Employee must match the linked performance questionnaire."
            if self.review_cycle_id != questionnaire.review_cycle_id:
                errors["review_cycle"] = "Review cycle must match the linked performance questionnaire."
        if self.employee_id and self.reporting_manager_id == self.employee_id:
            errors["reporting_manager"] = "Employees cannot evaluate their own potential."
        if self.employee_id and self.skip_level_manager_id == self.employee_id:
            errors["skip_level_manager"] = "Employees cannot evaluate their own potential."
        if errors:
            raise ValidationError(errors)

    def __str__(self):
        return f"{self.employee} Potential Assessment"

class EmployeePotentialQuestionnaireItem(models.Model):
    SOURCE_CHOICES = [
        ("automatic", "Automatic Assignment"),
        ("manual", "Admin Assignment"),
    ]
    assessment = models.ForeignKey(EmployeePotentialAssessment, on_delete=models.CASCADE, related_name="items")
    parameter = models.ForeignKey(PotentialParameter, on_delete=models.PROTECT, related_name="questionnaire_items")
    weightage = models.FloatField(default=1)
    order = models.IntegerField(default=0)
    source = models.CharField(max_length=20, choices=SOURCE_CHOICES, default="automatic")
    question_text_snapshot = models.TextField(null=True, blank=True)
    question_type_snapshot = models.CharField(max_length=20, choices=PotentialParameter.QUESTION_TYPES, null=True, blank=True)
    max_marks_snapshot = models.PositiveIntegerField(null=True, blank=True)
    category_name_snapshot = models.CharField(max_length=100, null=True, blank=True)

    class Meta:
        ordering = ["order", "id"]
        constraints = [
            models.UniqueConstraint(fields=["assessment", "parameter"], name="unique_potential_assessment_parameter"),
            models.CheckConstraint(check=Q(weightage__gt=0), name="positive_potential_item_weight")
        ]

    def clean(self):
        if self.weightage is None or not isfinite(self.weightage) or self.weightage <= 0:
            raise ValidationError({"weightage": "Weightage must be greater than zero."})

    @property
    def effective_question_type(self):
        return self.question_type_snapshot or self.parameter.question_type

    @property
    def effective_max_marks(self):
        if self.max_marks_snapshot is not None:
            return self.max_marks_snapshot
        return self.parameter.max_marks

    @property
    def effective_question_text(self):
        return self.question_text_snapshot or self.parameter.question_text

    def __str__(self):
        return f"Assessment {self.assessment_id} - Question {self.parameter_id}"

class EmployeePotentialQuestionnaireCustomization(models.Model):
    ACTION_CHOICES = [
        ("include", "Include"),
        ("exclude", "Exclude"),
    ]
    assessment = models.ForeignKey(EmployeePotentialAssessment, on_delete=models.CASCADE, related_name="customizations")
    parameter = models.ForeignKey(PotentialParameter, on_delete=models.CASCADE)
    action = models.CharField(max_length=10, choices=ACTION_CHOICES)
    created_by = models.ForeignKey(Employee, on_delete=models.SET_NULL, null=True, blank=True, related_name="potential_questionnaire_customizations")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["assessment", "parameter"], name="unique_potential_questionnaire_customization")
        ]

    def __str__(self):
        return f"{self.assessment_id} - {self.parameter_id} - {self.action}"

class EmployeePotentialResponse(models.Model):
    EVALUATOR_CHOICES = [
        ("rm", "RM"),
        ("skip", "Skip Manager"),
    ]
    assessment = models.ForeignKey(EmployeePotentialAssessment, on_delete=models.CASCADE)
    parameter = models.ForeignKey(PotentialParameter, on_delete=models.CASCADE)
    evaluator_type = models.CharField(max_length=10, choices=EVALUATOR_CHOICES)
    score = models.FloatField(null=True, blank=True)
    submitted_at = models.DateTimeField(auto_now=True)
    questionnaire_item = models.ForeignKey(EmployeePotentialQuestionnaireItem, on_delete=models.CASCADE, related_name="responses", null=True, blank=True)
    descriptive_answer = models.TextField(null=True, blank=True)
    is_not_applicable = models.BooleanField(default=False)
    submitted_by = models.ForeignKey(Employee, on_delete=models.SET_NULL, null=True, blank=True, related_name="submitted_potential_responses")

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["questionnaire_item", "evaluator_type"], condition=Q(questionnaire_item__isnull=False), name="unique_potential_item_evaluator_response"),
            models.CheckConstraint(check=Q(evaluator_type__in=["rm", "skip"]), name="valid_potential_response_evaluator")
        ]

    def clean(self):
        errors = {}
        if self.evaluator_type not in ("rm", "skip"):
            errors["evaluator_type"] = "Only RM and Skip Manager evaluations are supported."
        if self.questionnaire_item_id:
            item = self.questionnaire_item
            if item.assessment_id != self.assessment_id:
                errors["questionnaire_item"] = "Questionnaire item belongs to another assessment."
            if item.parameter_id != self.parameter_id:
                errors["parameter"] = "Parameter must match the questionnaire item."
            question_type = item.effective_question_type
            max_marks = item.effective_max_marks
        elif self.parameter_id:
            question_type = self.parameter.question_type
            max_marks = self.parameter.max_marks
        else:
            raise ValidationError({"parameter": "A potential question is required."})
        if self.assessment_id and self.submitted_by_id:
            assessment = self.assessment
            if self.submitted_by_id == assessment.employee_id:
                errors["submitted_by"] = "Employees cannot evaluate their own potential."
            expected_evaluator_id = assessment.reporting_manager_id if self.evaluator_type == "rm" else assessment.skip_level_manager_id
            if expected_evaluator_id and self.submitted_by_id != expected_evaluator_id:
                errors["submitted_by"] = "Employee is not assigned to this evaluator role."
        if self.is_not_applicable:
            if self.score is not None or (self.descriptive_answer or "").strip():
                errors["is_not_applicable"] = "N/A responses cannot contain a score or descriptive answer."
        elif question_type == "descriptive":
            if self.score is not None:
                errors["score"] = "Descriptive responses cannot contain numerical scores."
            if not (self.descriptive_answer or "").strip():
                errors["descriptive_answer"] = "A descriptive answer is required."
        else:
            if self.score is None:
                errors["score"] = "An objective score is required."
            elif not isfinite(self.score) or not 0 <= self.score <= max_marks:
                errors["score"] = f"Score must be between 0 and {max_marks}."
            if (self.descriptive_answer or "").strip():
                errors["descriptive_answer"] = "Objective responses cannot contain descriptive answers."
        if errors:
            raise ValidationError(errors)

    def __str__(self):
        return f"{self.parameter} - {self.evaluator_type}"

class EmployeePotentialDescriptiveScore(models.Model):
    response = models.OneToOneField(EmployeePotentialResponse, on_delete=models.CASCADE, related_name="admin_descriptive_score")
    score = models.PositiveIntegerField()
    scored_by = models.ForeignKey(Employee, on_delete=models.SET_NULL, null=True, related_name="assigned_potential_descriptive_scores")
    remarks = models.TextField(blank=True, default="")
    scored_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.CheckConstraint(check=Q(score__gte=1) & Q(score__lte=10), name="valid_potential_descriptive_score")
        ]

    def clean(self):
        if not self.response_id:
            raise ValidationError({"response": "A descriptive response is required."})
        response = self.response
        item = response.questionnaire_item
        question_type = item.effective_question_type if item else response.parameter.question_type
        max_marks = item.effective_max_marks if item else response.parameter.max_marks
        if question_type != "descriptive":
            raise ValidationError({"response": "Only descriptive responses can receive admin scores."})
        if response.is_not_applicable:
            raise ValidationError({"response": "N/A responses cannot receive admin scores."})
        if not (response.descriptive_answer or "").strip():
            raise ValidationError({"response": "Cannot score an empty descriptive answer."})
        assessment = response.assessment
        submitted = assessment.rm_submitted if response.evaluator_type == "rm" else assessment.skip_submitted
        if not submitted:
            raise ValidationError({"response": "Only submitted descriptive answers can be scored."})
        allowed_max = min(10, max_marks)
        if self.score is None or not 1 <= self.score <= allowed_max:
            raise ValidationError({"score": f"Score must be between 1 and {allowed_max}."})

    def save(self, *args, **kwargs):
        self.full_clean()
        super().save(*args, **kwargs)

    def __str__(self):
        return f"Potential Response {self.response_id}: {self.score}"

class EmployeePotentialDescriptiveAssessment(models.Model):
    STATUS_CHOICES = [
        ("draft", "Draft"),
        ("finalized", "Finalized"),
    ]
    assessment = models.OneToOneField(EmployeePotentialAssessment, on_delete=models.CASCADE, related_name="descriptive_assessment")
    is_enabled = models.BooleanField(default=False)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="draft")
    descriptive_score = models.FloatField(null=True, blank=True)
    objective_weight_used = models.FloatField(null=True, blank=True)
    descriptive_weight_used = models.FloatField(null=True, blank=True)
    assessed_by = models.ForeignKey(Employee, on_delete=models.SET_NULL, null=True, blank=True, related_name="potential_descriptive_assessments")
    assessed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"Potential Descriptive Assessment - {self.assessment_id}"

class EmployeePotentialScore(models.Model):
    employee = models.ForeignKey("employees.Employee", on_delete=models.CASCADE)
    review_cycle = models.ForeignKey("evaluations.ReviewCycle", on_delete=models.CASCADE)
    employee_questionnaire = models.OneToOneField("evaluations.EmployeeQuestionnaire", on_delete=models.CASCADE)
    rm_score = models.FloatField(null=True, blank=True)
    skip_score = models.FloatField(null=True, blank=True)
    final_potential_score = models.FloatField()
    calculated_at = models.DateTimeField(auto_now=True)
    effective_rm_weight = models.FloatField(null=True, blank=True)
    effective_skip_weight = models.FloatField(null=True, blank=True)
    objective_final_score = models.FloatField(null=True, blank=True)
    descriptive_final_score = models.FloatField(null=True, blank=True)
    descriptive_scoring_applied = models.BooleanField(default=False)
    effective_objective_component_weight = models.FloatField(null=True, blank=True)
    effective_descriptive_component_weight = models.FloatField(null=True, blank=True)

    def __str__(self):
        return f"{self.employee} Potential Score"

class EmployeeNineBoxPlacement(models.Model):
    employee = models.ForeignKey("employees.Employee", on_delete=models.CASCADE)
    review_cycle = models.ForeignKey("evaluations.ReviewCycle", on_delete=models.CASCADE)
    employee_questionnaire = models.OneToOneField("evaluations.EmployeeQuestionnaire", on_delete=models.CASCADE)
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
    employee = models.ForeignKey("employees.Employee", on_delete=models.CASCADE)
    review_cycle = models.ForeignKey("evaluations.ReviewCycle", on_delete=models.CASCADE)
    employee_questionnaire = models.OneToOneField("evaluations.EmployeeQuestionnaire", on_delete=models.CASCADE)
    nine_box_placement = models.ForeignKey("scoring.EmployeeNineBoxPlacement", on_delete=models.CASCADE)
    ppi_score_used = models.FloatField()
    potential_score_used = models.FloatField()
    box_label = models.CharField(max_length=100)
    min_appraisal_percent = models.FloatField()
    max_appraisal_percent = models.FloatField()
    suggested_appraisal_percent = models.FloatField()
    hr_override_percent = models.FloatField(null=True, blank=True)
    override_reason = models.TextField(null=True, blank=True)
    overridden_by = models.ForeignKey("employees.Employee", null=True, blank=True, on_delete=models.SET_NULL, related_name="appraisal_overrides")
    overridden_at = models.DateTimeField(null=True, blank=True)
    final_effective_appraisal_percent = models.FloatField()
    calculated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"{self.employee} - {self.review_cycle} - {self.final_effective_appraisal_percent}%"

class EmployeeAnalyticsRelease(models.Model):
    employee_questionnaire = models.OneToOneField("evaluations.EmployeeQuestionnaire", on_delete=models.CASCADE, related_name="analytics_release")
    is_released_to_employee = models.BooleanField(default=False)
    released_at = models.DateTimeField(null=True, blank=True)
    released_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="released_employee_analytics")
    unreleased_at = models.DateTimeField(null=True, blank=True)
    unreleased_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="unreleased_employee_analytics")
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
    employee_questionnaire = models.OneToOneField("evaluations.EmployeeQuestionnaire", on_delete=models.CASCADE, related_name="descriptive_assessment")
    is_enabled = models.BooleanField(default=False)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="draft")
    descriptive_score = models.FloatField(null=True, blank=True)
    objective_weight_used = models.FloatField(null=True, blank=True)
    descriptive_weight_used = models.FloatField(null=True, blank=True)
    assessed_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="descriptive_assessments")
    assessed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"Descriptive Assessment - {self.employee_questionnaire_id}"

class EmployeeDescriptiveQuestionRating(models.Model):
    assessment = models.ForeignKey(EmployeeDescriptiveAssessment, on_delete=models.CASCADE, related_name="question_ratings")
    questionnaire_item = models.ForeignKey("evaluations.EmployeeQuestionnaireItem", on_delete=models.CASCADE)
    rating = models.PositiveSmallIntegerField(null=True, blank=True)
    weightage = models.FloatField(default=1)
    remarks = models.TextField(null=True, blank=True)
    rated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["assessment", "questionnaire_item"], name="unique_descriptive_question_rating"),
            models.CheckConstraint(check=Q(rating__isnull=True) | (Q(rating__gte=1) & Q(rating__lte=10)), name="valid_descriptive_rating"),
            models.CheckConstraint(check=Q(weightage__gt=0), name="positive_descriptive_weight")
        ]

    def __str__(self):
        return f"Assessment {self.assessment_id} - Item {self.questionnaire_item_id}"

class EmployeeDescriptiveAssessmentHistory(models.Model):
    assessment = models.ForeignKey(EmployeeDescriptiveAssessment, on_delete=models.CASCADE, related_name="history")
    action = models.CharField(max_length=50)
    previous_status = models.CharField(max_length=20, null=True, blank=True)
    new_status = models.CharField(max_length=20, null=True, blank=True)
    previous_score = models.FloatField(null=True, blank=True)
    new_score = models.FloatField(null=True, blank=True)
    change_details = models.JSONField(default=dict, blank=True)
    changed_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="descriptive_assessment_changes")
    changed_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-changed_at", "-id"]

    def __str__(self):
        return f"Assessment {self.assessment_id} - {self.action}"

class EmployeePotentialDescriptiveAssessmentHistory(models.Model):
    assessment = models.ForeignKey(EmployeePotentialDescriptiveAssessment, on_delete=models.CASCADE, related_name="history")
    action = models.CharField(max_length=50)
    previous_status = models.CharField(max_length=20, null=True, blank=True)
    new_status = models.CharField(max_length=20, null=True, blank=True)
    previous_score = models.FloatField(null=True, blank=True)
    new_score = models.FloatField(null=True, blank=True)
    change_details = models.JSONField(default=dict, blank=True)
    changed_by = models.ForeignKey(Employee, on_delete=models.SET_NULL, null=True, blank=True, related_name="potential_descriptive_assessment_changes")
    changed_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-changed_at", "-id"]

    def __str__(self):
        return f"Potential Descriptive Assessment {self.assessment_id} - {self.action}"