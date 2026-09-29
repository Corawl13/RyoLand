"""Betting HTTP API: place a bet, view your bet history, and browse the public catalog of
bettable events/markets/selections."""
from django.utils import timezone
from rest_framework import status
from rest_framework.generics import ListAPIView
from rest_framework.pagination import PageNumberPagination
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import EventStatus, SportEvent
from .serializers import (
    BetSerializer,
    BetsFilterSerializer,
    EventSerializer,
    PlaceBetSerializer,
)
from .services import BetPlacementService


class PlaceBetView(APIView):
    """POST: place a single or combo bet. Idempotent on `idempotency_key`."""

    permission_classes = [IsAuthenticated]

    def post(self, request):
        req = PlaceBetSerializer(data=request.data)
        req.is_valid(raise_exception=True)
        legs = [(str(leg["selection_id"]), leg["odds"]) for leg in req.validated_data["selections"]]
        bet, created = BetPlacementService.place_bet(
            user=request.user,
            currency=req.validated_data["currency"],
            stake_amount=req.validated_data["stake_amount"],
            legs=legs,
            idempotency_key=req.validated_data["idempotency_key"],
        )
        code = status.HTTP_201_CREATED if created else status.HTTP_200_OK
        return Response(BetSerializer(bet).data, status=code)


class BetsPagination(PageNumberPagination):
    page_size = 20
    page_size_query_param = "page_size"
    max_page_size = 100


class BetListView(ListAPIView):
    """GET: the signed-in user's bet history, newest first. Filter with `?status=`."""

    permission_classes = [IsAuthenticated]
    serializer_class = BetSerializer
    pagination_class = BetsPagination

    def get_queryset(self):
        filters = BetsFilterSerializer(data=self.request.query_params)
        filters.is_valid(raise_exception=True)
        queryset = (
            self.request.user.bets.all()
            .prefetch_related("selections__selection__market", "selections__event")
            .order_by("-placed_at")
        )
        status_value = filters.validated_data.get("status")
        if status_value:
            queryset = queryset.filter(status=status_value)
        return queryset


class EventListView(ListAPIView):
    """GET: public catalog of upcoming/live events and their open markets. No auth
    required — browsing odds doesn't need a signed-in account."""

    permission_classes = [AllowAny]
    serializer_class = EventSerializer
    pagination_class = BetsPagination

    def get_queryset(self):
        return (
            SportEvent.objects.filter(status__in=(EventStatus.UPCOMING, EventStatus.LIVE))
            .exclude(status=EventStatus.UPCOMING, start_time__lte=timezone.now())
            .prefetch_related("markets__selections")
            .order_by("start_time")
        )
