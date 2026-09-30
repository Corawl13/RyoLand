"""Settlement HTTP API. Both endpoints are staff-only: submitting a result and triggering
payouts, and reading the settlement audit log, are operator actions — never player-facing.
"""
from django.shortcuts import get_object_or_404
from rest_framework.generics import ListAPIView
from rest_framework.pagination import PageNumberPagination
from rest_framework.permissions import IsAdminUser
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.betting.models import SportEvent

from .models import SettlementBatch
from .serializers import (
    BatchesFilterSerializer,
    SettlementBatchSerializer,
    SubmitResultSerializer,
)
from .services import EventSettlementService


class SettleEventView(APIView):
    """POST: submit the official result for an event and settle every bet it touches
    (or, resubmitted with the same result, safely re-run settlement — see services.py)."""

    permission_classes = [IsAdminUser]

    def post(self, request, event_id):
        event = get_object_or_404(SportEvent, pk=event_id)
        req = SubmitResultSerializer(data=request.data)
        req.is_valid(raise_exception=True)
        batch = EventSettlementService.settle_event(
            event=event,
            winning_selection_ids=[str(i) for i in req.validated_data["winning_selection_ids"]],
            voided_selection_ids=[str(i) for i in req.validated_data["voided_selection_ids"]],
            is_cancelled=req.validated_data["is_cancelled"],
            scores=req.validated_data["scores"],
            submitted_by=request.user,
        )
        return Response(SettlementBatchSerializer(batch).data)


class BatchesPagination(PageNumberPagination):
    page_size = 20
    page_size_query_param = "page_size"
    max_page_size = 100


class SettlementBatchListView(ListAPIView):
    """GET: settlement run history. Filter with `?event=` or `?status=`."""

    permission_classes = [IsAdminUser]
    serializer_class = SettlementBatchSerializer
    pagination_class = BatchesPagination

    def get_queryset(self):
        filters = BatchesFilterSerializer(data=self.request.query_params)
        filters.is_valid(raise_exception=True)
        queryset = SettlementBatch.objects.select_related("event").order_by("-created_at")
        if event_id := filters.validated_data.get("event"):
            queryset = queryset.filter(event_id=event_id)
        if status_value := filters.validated_data.get("status"):
            queryset = queryset.filter(status=status_value)
        return queryset
