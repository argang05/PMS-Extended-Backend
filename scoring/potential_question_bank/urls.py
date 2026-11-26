from django.urls import path
from .views import (
    PotentialCategoryListCreateView,
    PotentialCategoryDetailView,
    PotentialQuestionListCreateView,
    PotentialQuestionDetailView,
    PotentialQuestionBulkCreateView,
    PotentialQuestionBulkConfigureView,
    PotentialQuestionBulkDeleteView,
    PotentialTargetingOptionsView,
    PotentialTargetingPreviewView,
    PotentialQuestionTargetingPreviewView,
    PotentialQuestionBankStatsView,
)

app_name = "potential_question_bank"

urlpatterns = [
    path("categories/", PotentialCategoryListCreateView.as_view(), name="categories"),
    path("categories/<int:category_id>/", PotentialCategoryDetailView.as_view(), name="category-detail"),
    path("questions/", PotentialQuestionListCreateView.as_view(), name="questions"),
    path("questions/bulk-create/", PotentialQuestionBulkCreateView.as_view(), name="bulk-create"),
    path("questions/bulk-configure/", PotentialQuestionBulkConfigureView.as_view(), name="bulk-configure"),
    path("questions/bulk-delete/", PotentialQuestionBulkDeleteView.as_view(), name="bulk-delete"),
    path("questions/<int:question_id>/", PotentialQuestionDetailView.as_view(), name="question-detail"),
    path("questions/<int:question_id>/targeting-preview/", PotentialQuestionTargetingPreviewView.as_view(), name="question-targeting-preview"),
    path("targeting/options/", PotentialTargetingOptionsView.as_view(), name="targeting-options"),
    path("targeting/preview/", PotentialTargetingPreviewView.as_view(), name="targeting-preview"),
    path("stats/", PotentialQuestionBankStatsView.as_view(), name="stats"),
]