from django.urls import path
from .views import (
    PotentialDescriptivePreferenceView, PotentialDescriptiveWorkspaceView,
    PotentialDescriptiveReadinessView, PotentialDescriptiveSingleScoreView,
    PotentialDescriptiveBulkScoreView, PotentialDescriptiveClearScoreView,
    PotentialDescriptiveHistoryView
)

app_name = "potential_descriptive"

urlpatterns = [
    path("<int:assessment_id>/preference/", PotentialDescriptivePreferenceView.as_view(), name="preference"),
    path("<int:assessment_id>/workspace/", PotentialDescriptiveWorkspaceView.as_view(), name="workspace"),
    path("<int:assessment_id>/readiness/", PotentialDescriptiveReadinessView.as_view(), name="readiness"),
    path("<int:assessment_id>/score/", PotentialDescriptiveSingleScoreView.as_view(), name="score"),
    path("<int:assessment_id>/bulk-save/", PotentialDescriptiveBulkScoreView.as_view(), name="bulk-save"),
    path("<int:assessment_id>/clear-scores/", PotentialDescriptiveClearScoreView.as_view(), name="clear-scores"),
    path("<int:assessment_id>/history/", PotentialDescriptiveHistoryView.as_view(), name="history"),
]