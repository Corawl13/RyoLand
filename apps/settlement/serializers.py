from rest_framework import serializers

from .models import EventResult, SettlementBatch, SettlementStatus


class SubmitResultSerializer(serializers.Serializer):
    is_cancelled = serializers.BooleanField(default=False)
    winning_selection_ids = serializers.ListField(child=serializers.UUIDField(), required=False, default=list)
    voided_selection_ids = serializers.ListField(child=serializers.UUIDField(), required=False, default=list)
    scores = serializers.JSONField(required=False, default=dict)

    def validate(self, attrs):
        if not attrs["is_cancelled"] and not attrs["winning_selection_ids"]:
            raise serializers.ValidationError(
                {"winning_selection_ids": "Required unless is_cancelled is true."}
            )
        return attrs


class SettlementBatchSerializer(serializers.ModelSerializer):
    event_name = serializers.CharField(source="event.name", read_only=True)

    class Meta:
        model = SettlementBatch
        fields = (
            "id",
            "event",
            "event_name",
            "status",
            "total_bets_processed",
            "payout_totals",
            "errors_log",
            "created_at",
            "completed_at",
        )
        read_only_fields = fields


class BatchesFilterSerializer(serializers.Serializer):
    event = serializers.UUIDField(required=False)
    status = serializers.ChoiceField(choices=SettlementStatus.choices, required=False)


class EventResultSerializer(serializers.ModelSerializer):
    class Meta:
        model = EventResult
        fields = (
            "id",
            "event",
            "is_cancelled",
            "scores",
            "winning_selection_ids",
            "voided_selection_ids",
            "settled_at",
            "created_at",
        )
        read_only_fields = fields
