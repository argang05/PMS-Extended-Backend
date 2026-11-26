from django.urls import path, include
from .views import (
    AdminScoringQuestionnaireListView, CalculateScoreView,
    ScoreResultView, HRScoreOverrideView, ReleaseScoreView,
    MyScoreView, CalculatePPIView, PPIResultView
)
from .views_potential import (
    RMPotentialPendingView, SkipPotentialPendingView,
    RMPotentialFormView, SkipPotentialFormView,
    RMSavePotentialDraftView, SkipSavePotentialDraftView,
    RMSubmitPotentialView, SkipSubmitPotentialView,
    CalculatePotentialScoreView, PotentialResultView
)
from .views_ninebox import GenerateNineBoxPlacementView, NineBoxPlacementResultView
from .views_appraisal import (
    GenerateAppraisalRecommendationView,
    AppraisalRecommendationResultView,
    AppraisalRecommendationOverrideView
)
from .views_analytics_release import AnalyticsReleaseStatusView, AnalyticsReleaseToggleView, MyAnalyticsReportView
from .views_descriptive_preference import DescriptiveScoringPreferenceView
from .views_readiness import ScoringReadinessView

urlpatterns = [
    # Centralized Scoring Readiness
    path("readiness/<int:questionnaire_id>/", ScoringReadinessView.as_view(), name="scoring_readiness"),
    # Performance Scoring
    path("admin/questionnaires/", AdminScoringQuestionnaireListView.as_view()),
    path("calculate/<int:questionnaire_id>/", CalculateScoreView.as_view()),
    path("result/<int:questionnaire_id>/", ScoreResultView.as_view()),
    path("override/<int:questionnaire_id>/", HRScoreOverrideView.as_view()),
    path("release/<int:questionnaire_id>/", ReleaseScoreView.as_view()),
    path("my-score/<int:questionnaire_id>/", MyScoreView.as_view()),
    path("my-score/", MyScoreView.as_view()),
    # Performance Descriptive Scoring Preference
    path("descriptive-preference/<int:questionnaire_id>/", DescriptiveScoringPreferenceView.as_view(), name="descriptive_scoring_preference"),
    # Past Performance Index
    path("ppi/calculate/<int:questionnaire_id>/", CalculatePPIView.as_view()),
    path("ppi/result/<int:questionnaire_id>/", PPIResultView.as_view()),
    # Potential Question Bank, Questionnaire Builder and Evaluation
    path("potential/question-bank/", include("scoring.potential_question_bank.urls")),
    path("potential/questionnaires/", include("scoring.potential_questionnaire.urls")),
    path("potential/evaluations/", include("scoring.potential_evaluation.urls")),
    path("potential/descriptive/", include("scoring.potential_descriptive.urls")),
    # Existing Potential Evaluation Endpoints
    path("potential/rm/pending/", RMPotentialPendingView.as_view()),
    path("potential/skip/pending/", SkipPotentialPendingView.as_view()),
    path("potential/rm/form/<int:assessment_id>/", RMPotentialFormView.as_view()),
    path("potential/skip/form/<int:assessment_id>/", SkipPotentialFormView.as_view()),
    path("potential/rm/save/", RMSavePotentialDraftView.as_view()),
    path("potential/skip/save/", SkipSavePotentialDraftView.as_view()),
    path("potential/rm/submit/", RMSubmitPotentialView.as_view()),
    path("potential/skip/submit/", SkipSubmitPotentialView.as_view()),
    path("potential/calculate/<int:assessment_id>/", CalculatePotentialScoreView.as_view()),
    path("potential/result/<int:assessment_id>/", PotentialResultView.as_view()),
    # Nine Box Placement
    path("ninebox/generate/<int:questionnaire_id>/", GenerateNineBoxPlacementView.as_view()),
    path("ninebox/result/<int:questionnaire_id>/", NineBoxPlacementResultView.as_view()),
    # Appraisal Recommendation
    path("appraisal/generate/<int:questionnaire_id>/", GenerateAppraisalRecommendationView.as_view()),
    path("appraisal/result/<int:questionnaire_id>/", AppraisalRecommendationResultView.as_view()),
    path("appraisal/override/<int:questionnaire_id>/", AppraisalRecommendationOverrideView.as_view()),
    # Analytics Release
    path("analytics-release/<int:questionnaire_id>/", AnalyticsReleaseStatusView.as_view()),
    path("analytics-release/toggle/<int:questionnaire_id>/", AnalyticsReleaseToggleView.as_view()),
    path("my-analytics/<int:questionnaire_id>/", MyAnalyticsReportView.as_view(), name="my_analytics_report"),
]