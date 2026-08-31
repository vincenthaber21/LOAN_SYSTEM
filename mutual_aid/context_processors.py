from decimal import Decimal

from django.db.models import Sum

from .models import MutualAidClaim, MutualAidMembership


def mutual_aid_context(request):
    user = request.user
    if not user.is_authenticated:
        return {}

    if user.is_officer:
        pending_claim_count = MutualAidClaim.objects.filter(
            status__in=[MutualAidClaim.Status.SUBMITTED, MutualAidClaim.Status.UNDER_REVIEW]
        ).count()
        return {"pending_mutual_aid_claim_count": pending_claim_count}

    memberships = MutualAidMembership.objects.filter(
        member=user,
        status=MutualAidMembership.Status.ACTIVE,
    ).select_related("plan")
    total_contributed = memberships.aggregate(value=Sum("total_contributed"))["value"] or Decimal("0.00")
    return {
        "mutual_aid_membership_count": memberships.count(),
        "primary_mutual_aid_membership": memberships.first(),
        "total_mutual_aid_contributed": total_contributed,
    }
