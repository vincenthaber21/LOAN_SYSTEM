from django.conf import settings
from django.contrib import admin
from django.urls import include, path, re_path
from django.views.static import serve

urlpatterns = [
    path("admin/", admin.site.urls),
    path("", include("lending.urls")),
    path("", include("savings.urls")),
    path("", include("mutual_aid.urls")),
]

# Always map /media/ so uploaded photos, signatures, and documents load in
# local and hosted environments (django.conf.urls.static only works when DEBUG).
urlpatterns += [
    re_path(
        r"^media/(?P<path>.*)$",
        serve,
        {"document_root": settings.MEDIA_ROOT},
    ),
]