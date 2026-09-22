from django.urls import path

from . import views

urlpatterns = [
    # Member mutual aid
    path("mutual-aid/", views.mutual_aid_dashboard, name="mutual_aid_dashboard"),
    path("mutual-aid/<int:membership_id>/", views.mutual_aid_membership_detail, name="mutual_aid_membership_detail"),
    path("mutual-aid/<int:membership_id>/claim/", views.mutual_aid_file_claim, name="mutual_aid_file_claim"),
    path("mutual-aid/claims/<int:claim_id>/", views.mutual_aid_claim_detail, name="mutual_aid_claim_detail"),
    # Officer mutual aid
    path("officer/mutual-aid/", views.officer_mutual_aid_memberships, name="officer_mutual_aid_memberships"),
    path("officer/mutual-aid/enroll/", views.officer_enroll_mutual_aid, name="officer_enroll_mutual_aid"),
    path("officer/mutual-aid/available-plans/", views.officer_available_mutual_aid_plans, name="officer_available_mutual_aid_plans"),
    path(
        "officer/mutual-aid/<int:membership_id>/",
        views.officer_mutual_aid_membership_detail,
        name="officer_mutual_aid_membership_detail",
    ),
    path(
        "officer/mutual-aid/<int:membership_id>/edit/",
        views.officer_edit_mutual_aid_membership,
        name="officer_edit_mutual_aid_membership",
    ),
    path(
        "officer/mutual-aid/<int:membership_id>/delete/",
        views.officer_delete_mutual_aid_membership,
        name="officer_delete_mutual_aid_membership",
    ),
    path("officer/mutual-aid/plans/", views.officer_mutual_aid_plans, name="officer_mutual_aid_plans"),
    path("officer/mutual-aid/plans/add/", views.officer_add_mutual_aid_plan, name="officer_add_mutual_aid_plan"),
    path(
        "officer/mutual-aid/plans/<int:plan_id>/edit/",
        views.officer_edit_mutual_aid_plan,
        name="officer_edit_mutual_aid_plan",
    ),
    path("officer/mutual-aid/claims/", views.officer_mutual_aid_claims, name="officer_mutual_aid_claims"),
    path(
        "officer/mutual-aid/claims/<int:claim_id>/",
        views.officer_mutual_aid_claim_review,
        name="officer_mutual_aid_claim_review",
    ),
]
