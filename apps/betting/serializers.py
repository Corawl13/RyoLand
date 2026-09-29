from rest_framework import serializers

from apps.wallet.models import Currency

from .models import (
    Bet,
    BetSelection,
    BetStatus,
    Market,
    MarketStatus,
    Selection,
    SportEvent,
)


# ==========================================================================================
# Read: events / markets / selections (public catalog)
# ==========================================================================================
class SelectionSerializer(serializers.ModelSerializer):
    class Meta:
        model = Selection
        fields = ("id", "name", "current_odds")
        read_only_fields = fields


class MarketSerializer(serializers.ModelSerializer):
    selections = SelectionSerializer(many=True, read_only=True)

    class Meta:
        model = Market
        fields = ("id", "name", "market_type", "status", "selections")
        read_only_fields = fields


class EventSerializer(serializers.ModelSerializer):
    markets = serializers.SerializerMethodField()

    class Meta:
        model = SportEvent
        fields = ("id", "name", "sport", "start_time", "status", "markets")
        read_only_fields = fields

    def get_markets(self, obj: SportEvent):
        # Only OPEN markets: a suspended/settled market isn't something a browsing player
        # should be offered odds on.
        open_markets = [m for m in obj.markets.all() if m.status == MarketStatus.OPEN]
        return MarketSerializer(open_markets, many=True).data


# ==========================================================================================
# Read: bet history
# ==========================================================================================
class BetSelectionSerializer(serializers.ModelSerializer):
    selection_name = serializers.CharField(source="selection.name", read_only=True)
    market_name = serializers.CharField(source="selection.market.name", read_only=True)
    event_name = serializers.CharField(source="event.name", read_only=True)

    class Meta:
        model = BetSelection
        fields = ("id", "event_name", "market_name", "selection_name", "locked_odds", "status")
        read_only_fields = fields


class BetSerializer(serializers.ModelSerializer):
    selections = BetSelectionSerializer(many=True, read_only=True)

    class Meta:
        model = Bet
        fields = (
            "id",
            "bet_type",
            "status",
            "currency",
            "stake_amount",
            "total_odds",
            "potential_payout",
            "placed_at",
            "selections",
        )
        read_only_fields = fields


class BetsFilterSerializer(serializers.Serializer):
    status = serializers.ChoiceField(choices=BetStatus.choices, required=False)


# ==========================================================================================
# Write: place a bet
# ==========================================================================================
class BetLegInputSerializer(serializers.Serializer):
    """One leg as the client last saw it: which selection, and the odds displayed at the
    time — used only to detect drift (see OddsVerificationService); the odds actually
    locked into the ticket always come from the database, never from this value."""

    selection_id = serializers.UUIDField()
    odds = serializers.DecimalField(max_digits=12, decimal_places=4, min_value=1)


class PlaceBetSerializer(serializers.Serializer):
    currency = serializers.ChoiceField(choices=Currency.choices)
    stake_amount = serializers.DecimalField(max_digits=28, decimal_places=8, min_value=0)
    idempotency_key = serializers.CharField(max_length=255)
    selections = BetLegInputSerializer(many=True, min_length=1)

    def validate_selections(self, value):
        ids = [str(leg["selection_id"]) for leg in value]
        if len(set(ids)) != len(ids):
            raise serializers.ValidationError("The same selection was submitted more than once.")
        return value
