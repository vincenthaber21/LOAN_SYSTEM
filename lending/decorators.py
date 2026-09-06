from functools import wraps

from django.contrib import messages
from django.shortcuts import redirect

from .models import User


def role_required(role):
    def decorator(view_func):
        @wraps(view_func)
        def wrapped(request, *args, **kwargs):
            if not request.user.is_authenticated:
                return redirect("login")
            if role == "officer" and not request.user.is_officer:
                messages.error(request, "That workspace is only available to loan officers.")
                return redirect("dashboard")
            if role == "member" and request.user.is_officer:
                messages.error(request, "Please use the officer workspace.")
                return redirect("officer_dashboard")
            if role == "admin" and not request.user.is_admin:
                messages.error(request, "That action is only available to administrators.")
                return redirect("officer_dashboard" if request.user.is_officer else "dashboard")
            if role == "manager" and not request.user.is_manager:
                messages.error(request, "That action is only available to managers.")
                return redirect("officer_dashboard" if request.user.is_officer else "dashboard")
            return view_func(request, *args, **kwargs)

        return wrapped

    return decorator