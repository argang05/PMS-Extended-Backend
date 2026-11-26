from django.urls import path
from .views import (
    PotentialEvaluationListView, PotentialEvaluationDetailView,
    PotentialLegacyEvaluationPrepareView, PotentialEvaluationSubmitView
)

app_name = "potential_evaluation"

urlpatterns = [
    path("", PotentialEvaluationListView.as_view(), name="list"),
    path("<int:assessment_id>/", PotentialEvaluationDetailView.as_view(), name="detail"),
    path("<int:assessment_id>/prepare/", PotentialLegacyEvaluationPrepareView.as_view(), name="prepare"),
    path("<int:assessment_id>/submit/", PotentialEvaluationSubmitView.as_view(), name="submit"),
]