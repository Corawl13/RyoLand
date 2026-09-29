from django.urls import path

from . import views

app_name = "betting"

urlpatterns = [
    path("betting/bets/place/", views.PlaceBetView.as_view(), name="place-bet"),
    path("betting/bets/", views.BetListView.as_view(), name="bets"),
    path("betting/events/", views.EventListView.as_view(), name="events"),
]
