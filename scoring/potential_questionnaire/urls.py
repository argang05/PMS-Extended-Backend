from django.urls import path
from .views import (
    PotentialQuestionnaireOptionsView,
    PotentialQuestionnaireListCreateView,
    PotentialQuestionnaireDetailView,
    PotentialQuestionnaireAvailableQuestionsView,
    PotentialQuestionnaireSaveView,
    PotentialQuestionnaireSyncView,
    PotentialQuestionnaireResetView,
    PotentialQuestionnaireCopyView,
    PotentialQuestionnairePublishView,
)

app_name = "potential_questionnaire"

urlpatterns = [
    path("options/", PotentialQuestionnaireOptionsView.as_view(), name="options"),
    path("", PotentialQuestionnaireListCreateView.as_view(), name="list-create"),
    path("<int:assessment_id>/", PotentialQuestionnaireDetailView.as_view(), name="detail"),
    path("<int:assessment_id>/available-questions/", PotentialQuestionnaireAvailableQuestionsView.as_view(), name="available-questions"),
    path("<int:assessment_id>/save/", PotentialQuestionnaireSaveView.as_view(), name="save"),
    path("<int:assessment_id>/sync/", PotentialQuestionnaireSyncView.as_view(), name="sync"),
    path("<int:assessment_id>/reset/", PotentialQuestionnaireResetView.as_view(), name="reset"),
    path("<int:assessment_id>/copy/", PotentialQuestionnaireCopyView.as_view(), name="copy"),
    path("<int:assessment_id>/publish/", PotentialQuestionnairePublishView.as_view(), name="publish"),
]