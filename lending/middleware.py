from django.utils import timezone

from .models import User


class LastSeenMiddleware:
    """Track when officer accounts were last active in the workspace."""

    UPDATE_INTERVAL_SECONDS = 60

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        user = request.user
        if user.is_authenticated and getattr(user, "is_officer", False):
            now = timezone.now()
            last_update = request.session.get("_last_seen_update")
            if not last_update or now.timestamp() - last_update >= self.UPDATE_INTERVAL_SECONDS:
                User.objects.filter(pk=user.pk).update(last_seen=now)
                request.session["_last_seen_update"] = now.timestamp()
        return response
