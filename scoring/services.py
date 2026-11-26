from collections import defaultdict
from math import isfinite
from django.core.exceptions import ValidationError
from evaluations.models import (
    EmployeeQuestionnaireItem,
    EmployeeQuestionnaireStageSubmission,
    EmployeeQuestionnaireResponse
)
from .models import ScoreConfiguration
from .descriptive_preference import is_descriptive_scoring_enabled

EVALUATORS = ("self", "rm", "skip", "peer")
CATEGORIES = ("behavioural", "performance")
COMPONENTS = ("objective", "descriptive")


def get_active_config():
    return ScoreConfiguration.objects.filter(is_active=True).order_by("-id").first()


def _number(value, label):
    if value is None or isinstance(value, bool):
        raise ValidationError(f"{label} must be numeric.")
    try:
        value = float(value)
    except (TypeError, ValueError, OverflowError):
        raise ValidationError(f"{label} must be numeric.")
    if not isfinite(value):
        raise ValidationError(f"{label} must be finite.")
    return value


def _question_type(item):
    return str(item.question_type_snapshot or item.parameter.question_type).strip().lower()


def _category_type(item):
    value = str(item.category_type_snapshot or item.parameter.category.category_type).strip().lower()
    return "behavioural" if value == "behavioral" else value


def _max_marks(item):
    return item.max_marks_snapshot if item.max_marks_snapshot is not None else item.parameter.max_marks


def _weighted(entries):
    total = sum(weight for _, weight in entries)
    return sum(score * weight for score, weight in entries) / total if total > 0 else None


def weighted_category_score(responses):
    entries = []
    for response in responses:
        if response.is_not_applicable:
            continue
        if response.score is None:
            return None
        weight = _number(response.questionnaire_item.weightage, "Question weight")
        if weight <= 0:
            raise ValidationError("Question weight must be positive.")
        entries.append((_number(response.score, "Response score"), weight))
    result = _weighted(entries)
    return round(result, 2) if result is not None else None


def calculate_evaluator_score(behavioural, performance, config):
    if behavioural is None or performance is None:
        return None
    return round(
        behavioural * config.behavioural_weight +
        performance * config.performance_weight, 2
    )


def normalize_weights(active_evaluators, config):
    weights = {
        "self": config.self_weight,
        "rm": config.rm_weight,
        "skip": config.skip_weight,
        "peer": config.peer_weight
    }
    active = list(dict.fromkeys(e for e in active_evaluators if e in weights))
    total = sum(_number(weights[e], f"{e} weight") for e in active)
    return {e: weights[e] / total for e in active} if total > 0 else {}


def calculate_final_score(scores, weights):
    return round(sum(
        score * weights[e] for e, score in scores.items()
        if score is not None and e in weights
    ), 2)


def _validate_config(config):
    groups = (
        (config.behavioural_weight, config.performance_weight),
        (config.self_weight, config.rm_weight, config.skip_weight, config.peer_weight),
        (config.objective_component_weight, config.descriptive_component_weight)
    )
    for group in groups:
        numbers = [_number(value, "Configuration weight") for value in group]
        if any(not 0 <= value <= 1 for value in numbers) or abs(sum(numbers) - 1) > 1e-6:
            raise ValidationError(
                "Scoring configuration weights must be between 0 and 1 and each group must total 1."
            )


def _skip_required(questionnaire):
    manager = questionnaire.employee.reporting_manager
    return not str(getattr(manager, "band", "") or "").upper().startswith("SM")


def _peer_ids(questionnaire):
    assigned = set(questionnaire.peer_assignments.values_list("peer_id", flat=True))
    if not assigned and questionnaire.peer_reviewer_id:
        assigned.add(questionnaire.peer_reviewer_id)
    return assigned


def _item_map(questionnaire):
    items = list(
        EmployeeQuestionnaireItem.objects.filter(
            employee_questionnaire=questionnaire
        ).select_related("parameter__category").order_by("order", "id")
    )
    if not items:
        raise ValidationError("Questionnaire contains no items.")

    result = {}
    for item in items:
        component, category = _question_type(item), _category_type(item)
        weight = _number(item.weightage, "Question weight")
        maximum = _number(_max_marks(item), "Maximum marks")
        if component not in COMPONENTS or category not in CATEGORIES or weight <= 0 or maximum <= 0:
            raise ValidationError(
                f"Invalid classification, weight, or maximum marks on item {item.id}."
            )
        result[item.id] = (component, category, weight, maximum)
    return result


def _submission_scores(submission, item_map, descriptive_enabled):
    responses = list(
        EmployeeQuestionnaireResponse.objects.filter(
            submission=submission
        ).select_related("admin_descriptive_score")
    )
    by_item = {r.questionnaire_item_id: r for r in responses}
    missing = []

    if len(by_item) != len(responses):
        missing.append({"submission_id": submission.id, "reason": "Duplicate responses"})

    if set(by_item) - set(item_map):
        missing.append({
            "submission_id": submission.id,
            "reason": "Responses to foreign questionnaire items"
        })

    groups = defaultdict(list)

    for item_id, (component, category, weight, maximum) in item_map.items():
        response = by_item.get(item_id)

        if response is None:
            missing.append({
                "submission_id": submission.id,
                "item_id": item_id,
                "reason": "Response missing"
            })
            continue

        if response.reviewer_type != submission.evaluator_type:
            missing.append({
                "submission_id": submission.id,
                "item_id": item_id,
                "reason": "Reviewer type mismatch"
            })
            continue

        if response.is_not_applicable:
            if submission.evaluator_type != "peer" or response.score is not None or (response.descriptive_answer or "").strip():
                missing.append({
                    "submission_id": submission.id,
                    "item_id": item_id,
                    "reason": "Invalid N/A response"
                })
            continue

        if component == "descriptive":
            if not (response.descriptive_answer or "").strip():
                missing.append({
                    "submission_id": submission.id,
                    "item_id": item_id,
                    "reason": "Descriptive answer missing"
                })
                continue
            if not descriptive_enabled:
                continue
            record = getattr(response, "admin_descriptive_score", None)
            raw = record.score if record else None
        else:
            raw = response.score

        if raw is None:
            missing.append({
                "submission_id": submission.id,
                "item_id": item_id,
                "component": component,
                "reason": "Admin descriptive score pending" if component == "descriptive" else "Objective score missing"
            })
            continue

        score = _number(raw, "Response score")

        if not 0 <= score <= maximum:
            missing.append({
                "submission_id": submission.id,
                "item_id": item_id,
                "reason": "Score outside maximum marks"
            })
            continue

        groups[(component, category)].append((score * 10 / maximum, weight))

    return {
        component: {
            category: _weighted(groups[(component, category)])
            for category in CATEGORIES
        }
        for component in COMPONENTS
    }, missing


def _category_combined(categories, config):
    weights = {
        "behavioural": config.behavioural_weight,
        "performance": config.performance_weight
    }
    return _weighted([
        (value, weights[key])
        for key, value in categories.items()
        if value is not None and weights[key] > 0
    ])


def calculate_questionnaire_scores(questionnaire, config=None):
    config = config or get_active_config()
    if config is None:
        raise ValidationError("No active scoring configuration.")

    _validate_config(config)
    item_map = _item_map(questionnaire)
    peer_ids = _peer_ids(questionnaire)
    skip_required = _skip_required(questionnaire)
    descriptive_enabled = is_descriptive_scoring_enabled(questionnaire)

    components_present = {component for component, _, _, _ in item_map.values()}
    required_components = ["objective"] + (
        ["descriptive"] if descriptive_enabled and "descriptive" in components_present else []
    )

    if "objective" not in components_present:
        required_components = ["descriptive"] if descriptive_enabled else []

    missing = []
    if not required_components:
        missing.append({
            "reason": "No enabled scoring component exists in this questionnaire"
        })

    submissions = list(
        EmployeeQuestionnaireStageSubmission.objects.filter(
            employee_questionnaire=questionnaire,
            status="submitted",
            evaluator_type__in=EVALUATORS
        ).order_by("id")
    )

    selected = defaultdict(list)
    for submission in submissions:
        if submission.evaluator_type == "peer" and submission.submitted_by_id not in peer_ids:
            continue
        if submission.evaluator_type == "skip" and not skip_required:
            continue
        selected[submission.evaluator_type].append(submission)

    for evaluator in ("self", "rm") + (("skip",) if skip_required else ()):
        if len(selected[evaluator]) != 1:
            missing.append({
                "evaluator": evaluator,
                "reason": "Required submission missing or duplicated"
            })

    if not peer_ids:
        missing.append({"evaluator": "peer", "reason": "No peer reviewer assigned"})

    submitted_peers = [s.submitted_by_id for s in selected["peer"]]
    for peer_id in sorted(peer_ids):
        if submitted_peers.count(peer_id) != 1:
            missing.append({
                "evaluator": "peer",
                "peer_id": peer_id,
                "reason": "Peer submission missing or duplicated"
            })

    per_evaluator = {}
    category_scores, evaluator_scores = {}, {}
    component_evaluator_scores = {component: {} for component in COMPONENTS}
    peer_individual_scores = []

    for evaluator in EVALUATORS:
        records = []
        for submission in selected[evaluator]:
            scores, errors = _submission_scores(submission, item_map, descriptive_enabled)
            missing.extend(errors)
            records.append(scores)

        if not records:
            continue

        for component in COMPONENTS:
            for category in CATEGORIES:
                values = [
                    record[component][category] for record in records
                    if record[component][category] is not None
                ]
                category_scores[f"{evaluator}_{category}_{component}"] = (
                    round(sum(values) / len(values), 2) if values else None
                )

            values = [_category_combined(record[component], config) for record in records]
            values = [value for value in values if value is not None]
            component_evaluator_scores[component][evaluator] = (
                sum(values) / len(values) if values else None
            )

        per_evaluator[evaluator] = records

    active = [evaluator for evaluator in EVALUATORS if evaluator in per_evaluator]
    effective = normalize_weights(active, config)

    if not effective:
        missing.append({"reason": "No positive active evaluator weights"})

    component_finals = {}
    for component in required_components:
        values = component_evaluator_scores[component]
        if any(values.get(evaluator) is None for evaluator in active if effective.get(evaluator, 0) > 0):
            missing.append({
                "component": component,
                "reason": "Scoring component missing for a required evaluator"
            })
            component_finals[component] = None
        else:
            component_finals[component] = (
                sum(
                    values[evaluator] * effective[evaluator]
                    for evaluator in active if effective.get(evaluator, 0) > 0
                ) if effective else None
            )

    objective = component_finals.get("objective")
    descriptive = component_finals.get("descriptive")
    component_weights = {"objective": 0.0, "descriptive": 0.0}

    if len(required_components) == 1:
        component_weights[required_components[0]] = 1.0
    elif len(required_components) == 2:
        component_weights = {
            "objective": config.objective_component_weight,
            "descriptive": config.descriptive_component_weight
        }

    for evaluator in EVALUATORS:
        if evaluator not in per_evaluator:
            evaluator_scores[evaluator] = None
            continue

        if any(
            component_evaluator_scores[component].get(evaluator) is None
            for component in required_components if component_weights[component] > 0
        ):
            evaluator_scores[evaluator] = None
        else:
            evaluator_scores[evaluator] = round(sum(
                component_evaluator_scores[component][evaluator] * component_weights[component]
                for component in required_components if component_weights[component] > 0
            ), 2)

    for record in per_evaluator.get("peer", []):
        parts = {
            component: _category_combined(record[component], config)
            for component in required_components
        }
        value = (
            sum(
                parts[component] * component_weights[component]
                for component in required_components if component_weights[component] > 0
            )
            if all(
                parts[component] is not None
                for component in required_components if component_weights[component] > 0
            ) else None
        )
        peer_individual_scores.append(round(value, 2) if value is not None else None)

    final = (
        sum(
            component_finals[component] * component_weights[component]
            for component in required_components if component_weights[component] > 0
        )
        if required_components and all(
            component_finals[component] is not None
            for component in required_components if component_weights[component] > 0
        ) else None
    )

    if final is None:
        missing.append({"reason": "Final score unavailable"})

    return {
        "is_complete": not missing and final is not None,
        "missing_requirements": missing,
        "category_scores": category_scores,
        "evaluator_scores": evaluator_scores,
        "component_evaluator_scores": component_evaluator_scores,
        "peer_individual_scores": peer_individual_scores,
        "objective_final_score": round(objective, 2) if objective is not None else None,
        "descriptive_final_score": round(descriptive, 2) if descriptive is not None else None,
        "descriptive_scoring_applied": descriptive_enabled and "descriptive" in required_components,
        "effective_weights": {component: effective for component in required_components},
        "effective_objective_component_weight": component_weights["objective"],
        "effective_descriptive_component_weight": component_weights["descriptive"],
        "system_final_score": round(final, 2) if not missing and final is not None else None
    }