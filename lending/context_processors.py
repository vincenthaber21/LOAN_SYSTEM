from django.conf import settings

from .logo_utils import logo_mark_url
from .models import Features, LoanApplication, Notification


def product_context(request):
    features = Features.load()
    user = request.user
    is_officer = user.is_authenticated and user.is_officer
    is_admin = user.is_authenticated and user.is_admin
    is_manager = user.is_authenticated and user.is_manager
    route_name = getattr(getattr(request, "resolver_match", None), "url_name", "")
    active_nav = {
        "officer_dashboard": "dashboard",
        "borrower_dashboard": "dashboard",
        "applications_queue": "applications",
        "application_review": "applications",
        "borrowers": "borrowers",
        "all_members": "all_members",
        "borrower_detail": "all_members",
        "edit_member": "all_members",
        "add_member": "all_members",
        "loan_officers": "loan_officers",
        "add_officer": "loan_officers",
        "officer_activity_log": "loan_officers",
        "managers": "managers",
        "add_manager": "managers",
        "edit_manager": "managers",
        "manager_activity_log": "managers",
        "disbursements": "disbursements",
        "disbursement_detail": "disbursements",
        "disbursement_receipt": "disbursements",
        "reports": "reports",
        "loan_products": "products",
        "add_loan_product_page": "products",
        "edit_loan_product": "products",
        "loan_application": "apply",
        "loan_detail": "loan",
        "my_loan": "loan",
        "repayment_schedule": "schedule",
        "my_schedule": "schedule",
        "savings_dashboard": "savings",
        "savings_open_account": "savings",
        "savings_account_detail": "savings",
        "savings_deposit": "savings",
        "savings_withdraw": "savings",
        "mutual_aid_dashboard": "mutual_aid",
        "mutual_aid_membership_detail": "mutual_aid",
        "mutual_aid_file_claim": "mutual_aid",
        "mutual_aid_claim_detail": "mutual_aid",
        "officer_savings_accounts": "savings",
        "officer_savings_account_detail": "savings",
        "officer_open_savings_account": "savings",
        "officer_savings_products": "savings_products",
        "officer_add_savings_product": "savings_products",
        "officer_edit_savings_product": "savings_products",
        "officer_mutual_aid_memberships": "mutual_aid",
        "officer_enroll_mutual_aid": "mutual_aid",
        "officer_mutual_aid_membership_detail": "mutual_aid",
        "officer_mutual_aid_claims": "mutual_aid",
        "officer_mutual_aid_claim_review": "mutual_aid",
        "officer_mutual_aid_plans": "mutual_aid_plans",
        "officer_add_mutual_aid_plan": "mutual_aid_plans",
        "officer_edit_mutual_aid_plan": "mutual_aid_plans",
        "notifications": "notifications",
    }.get(route_name)
    unread_notification_count = 0
    if user.is_authenticated:
        unread_notification_count = Notification.objects.filter(user=user, is_read=False).count()
    return {
        "currency_symbol": "₱",
        "app_name": features.store_name,
        "app_tagline": features.tagline,
        "site_logo": features.logo,
        "site_logo_mark": logo_mark_url(features.logo),
        "media_url": settings.MEDIA_URL,
        "is_officer": is_officer,
        "is_admin": is_admin,
        "is_manager": is_manager,
        "user_initials": user.initials if user.is_authenticated else "KAP",
        "active_nav": active_nav,
        "unread_notification_count": unread_notification_count,
        "pending_application_count": (
            LoanApplication.objects.filter(status__in=["submitted", "under_review"]).count()
            if is_officer
            else 0
        ),
        "active_loan": user.current_loan if user.is_authenticated and not is_officer else None,
    }