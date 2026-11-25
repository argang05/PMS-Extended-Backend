from django.urls import path
from .views import *
from .peer_views import (
    PeerAssignmentListView, PeerAssignmentAddView,
    PeerAssignmentRemoveView, PeerReviewProgressView, PeerMyDraftView
)
from .descriptive_views import (
    AdminDescriptiveResponsesView, AdminSaveDescriptiveScoreView,
    AdminBulkDescriptiveScoreView
)

urlpatterns = [
    path("self-questionnaire/", SelfQuestionnaireView.as_view()),
    path("self-questionnaire/save/", SaveSelfEvaluation.as_view()),
    path("self-questionnaire/submit/", SubmitSelfEvaluation.as_view()),
    path("self-review-status/", SelfReviewStatusView.as_view()),
    path("recommend-peer/", RecommendPeerView.as_view()),

    path("rm/pending-reviews/", ReportingManagerQueueView.as_view()),
    path("rm/review-form/<int:questionnaire_id>/", RMReviewFormView.as_view()),
    path("rm/self-responses/<int:questionnaire_id>/", RMViewSelfResponses.as_view()),
    path("rm/save-draft/", RMSaveDraftView.as_view()),
    path("rm/submit/", RMSubmitReviewView.as_view()),

    path("skip/pending-reviews/", SkipPendingReviewsView.as_view(), name="skip_pending_reviews"),
    path("skip/review-form/<int:questionnaire_id>/", SkipReviewFormView.as_view(), name="skip_review_form"),
    path("skip/self-responses/<int:questionnaire_id>/", SkipSelfResponsesView.as_view(), name="skip_self_responses"),
    path("skip/rm-responses/<int:questionnaire_id>/", SkipRMResponsesView.as_view(), name="skip_rm_responses"),
    path("skip/save-draft/", SkipSaveDraftView.as_view(), name="skip_save_draft"),
    path("skip/submit/", SkipSubmitReviewView.as_view(), name="skip_submit_review"),

    path("peer/search/", PeerEmployeeSearchView.as_view()),
    path("peer/recommend/", PeerRecommendView.as_view()),
    path("peer/pending-reviews/", PeerPendingReviewsView.as_view()),
    path("peer/review-form/<int:questionnaire_id>/", PeerReviewFormView.as_view()),
    path("peer/save-draft/", PeerSaveDraftView.as_view()),
    path("peer/submit/", PeerSubmitReviewView.as_view()),
    path("peer/self-responses/<int:questionnaire_id>/", PeerSelfResponsesView.as_view()),

    path("peer/assignments/<int:questionnaire_id>/", PeerAssignmentListView.as_view(), name="peer_assignments"),
    path("peer/assignments/<int:questionnaire_id>/add/", PeerAssignmentAddView.as_view(), name="peer_assignment_add"),
    path("peer/assignments/<int:questionnaire_id>/remove/<int:peer_id>/", PeerAssignmentRemoveView.as_view(), name="peer_assignment_remove"),
    path("peer/progress/<int:questionnaire_id>/", PeerReviewProgressView.as_view(), name="peer_review_progress"),
    path("peer/my-draft/<int:questionnaire_id>/", PeerMyDraftView.as_view(), name="peer_my_draft"),

    path("admin/delete-questionnaire/<int:questionnaire_id>/", DeleteEmployeeQuestionnaireView.as_view(), name="delete_questionnaire"),
    path("admin/descriptive-responses/<int:questionnaire_id>/", AdminDescriptiveResponsesView.as_view(), name="admin_descriptive_responses"),
    path("admin/descriptive-score/save/", AdminSaveDescriptiveScoreView.as_view(), name="admin_descriptive_score_save"),
    path("admin/descriptive-score/bulk-save/", AdminBulkDescriptiveScoreView.as_view(), name="admin_descriptive_score_bulk_save"),
]