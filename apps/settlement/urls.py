from django.urls import path

from . import views

app_name = "settlement"

urlpatterns = [
    path("settlement/events/<uuid:event_id>/settle/", views.SettleEventView.as_view(), name="settle-event"),
    path("settlement/batches/", views.SettlementBatchListView.as_view(), name="batches"),
]
