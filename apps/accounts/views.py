"""HTTP endpoints for authentication and account management."""
from __future__ import annotations

from rest_framework import status
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.settings import api_settings as drf_settings
from rest_framework.views import APIView
from rest_framework_simplejwt.exceptions import InvalidToken, TokenError
from rest_framework_simplejwt.serializers import TokenRefreshSerializer
from rest_framework_simplejwt.settings import api_settings as jwt_settings
from rest_framework_simplejwt.tokens import RefreshToken

from . import services
from .models import AuthChallenge, User
from .serializers import (
    AuthResponseSerializer,
    EVMWalletProofSerializer,
    LinkedWalletSerializer,
    TelegramMiniAppAuthSerializer,
    TelegramWidgetAuthSerializer,
    TonWalletProofSerializer,
    UserProfileUpdateSerializer,
    UserSerializer,
    WalletChallengeRequestSerializer,
    WalletChallengeSerializer,
)


def _client_ip(request) -> str | None:
    xff = request.META.get("HTTP_X_FORWARDED_FOR")
    remote_addr = request.META.get("REMOTE_ADDR")
    num_proxies = drf_settings.NUM_PROXIES
    if num_proxies is None:
        return "".join(xff.split()) if xff else remote_addr
    if num_proxies == 0 or xff is None:
        return remote_addr
    addrs = xff.split(",")
    return addrs[-min(num_proxies, len(addrs))].strip()


def _auth_response(result) -> Response:
    body = AuthResponseSerializer(result).data
    response = Response(body, status=status.HTTP_201_CREATED if result.created else status.HTTP_200_OK)
    response["Cache-Control"] = "no-store"
    return response


class _PublicAPIView(APIView):
    permission_classes = [AllowAny]
    authentication_classes = []

    def get_authenticate_header(self, request):
        return "Bearer"


class WalletChallengeView(_PublicAPIView):
    throttle_scope = "auth_challenge"

    def post(self, request):
        req = WalletChallengeRequestSerializer(data=request.data)
        req.is_valid(raise_exception=True)
        challenge = services.create_wallet_challenge(
            chain=req.validated_data["chain"],
            address=req.validated_data["address"],
            ip_address=_client_ip(request),
        )
        return Response(WalletChallengeSerializer(challenge).data, status=status.HTTP_201_CREATED)


class WalletLinkChallengeView(APIView):
    permission_classes = [IsAuthenticated]
    throttle_scope = "auth_link"

    def post(self, request):
        req = WalletChallengeRequestSerializer(data=request.data)
        req.is_valid(raise_exception=True)
        challenge = services.create_wallet_challenge(
            chain=req.validated_data["chain"],
            address=req.validated_data["address"],
            purpose=AuthChallenge.Purpose.LINK_WALLET,
            user=request.user,
            ip_address=_client_ip(request),
        )
        return Response(WalletChallengeSerializer(challenge).data, status=status.HTTP_201_CREATED)


class EVMLoginView(_PublicAPIView):
    throttle_scope = "auth_login"

    def post(self, request):
        req = EVMWalletProofSerializer(data=request.data)
        req.is_valid(raise_exception=True)
        return _auth_response(services.login_with_evm(**req.validated_data))


class TonLoginView(_PublicAPIView):
    throttle_scope = "auth_login"

    def post(self, request):
        req = TonWalletProofSerializer(data=request.data)
        req.is_valid(raise_exception=True)
        return _auth_response(services.login_with_ton(proof=req.to_proof()))


class TelegramMiniAppLoginView(_PublicAPIView):
    throttle_scope = "auth_login"

    def post(self, request):
        req = TelegramMiniAppAuthSerializer(data=request.data)
        req.is_valid(raise_exception=True)
        return _auth_response(services.login_with_telegram_mini_app(**req.validated_data))


class TelegramWidgetLoginView(_PublicAPIView):
    throttle_scope = "auth_login"

    def post(self, request):
        req = TelegramWidgetAuthSerializer(data=request.data)
        req.is_valid(raise_exception=True)
        return _auth_response(services.login_with_telegram_widget(**req.validated_data))


class EVMLinkView(APIView):
    permission_classes = [IsAuthenticated]
    throttle_scope = "auth_link"

    def post(self, request):
        req = EVMWalletProofSerializer(data=request.data)
        req.is_valid(raise_exception=True)
        wallet = services.link_evm_wallet(user=request.user, **req.validated_data)
        return Response(LinkedWalletSerializer(wallet).data, status=status.HTTP_201_CREATED)


class TonLinkView(APIView):
    permission_classes = [IsAuthenticated]
    throttle_scope = "auth_link"

    def post(self, request):
        req = TonWalletProofSerializer(data=request.data)
        req.is_valid(raise_exception=True)
        wallet = services.link_ton_wallet(user=request.user, proof=req.to_proof())
        return Response(LinkedWalletSerializer(wallet).data, status=status.HTTP_201_CREATED)


class TelegramMiniAppLinkView(APIView):
    permission_classes = [IsAuthenticated]
    throttle_scope = "auth_link"

    def post(self, request):
        req = TelegramMiniAppAuthSerializer(data=request.data)
        req.is_valid(raise_exception=True)
        user = services.link_telegram_mini_app(user=request.user, **req.validated_data)
        return Response(UserSerializer(user).data)


class TelegramWidgetLinkView(APIView):
    permission_classes = [IsAuthenticated]
    throttle_scope = "auth_link"

    def post(self, request):
        req = TelegramWidgetAuthSerializer(data=request.data)
        req.is_valid(raise_exception=True)
        user = services.link_telegram_widget(user=request.user, **req.validated_data)
        return Response(UserSerializer(user).data)


class WalletUnlinkView(APIView):
    permission_classes = [IsAuthenticated]

    def delete(self, request, wallet_id):
        services.unlink_wallet(user=request.user, wallet_id=wallet_id)
        return Response(status=status.HTTP_204_NO_CONTENT)


class MeView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        return Response(UserSerializer(request.user).data)

    def patch(self, request):
        serializer = UserProfileUpdateSerializer(request.user, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(UserSerializer(request.user).data)


class LogoutView(_PublicAPIView):
    def post(self, request):
        token = request.data.get("refresh")
        if token:
            try:
                RefreshToken(token).blacklist()
            except TokenError:
                pass
        return Response(status=status.HTTP_204_NO_CONTENT)


class TokenRefreshView(_PublicAPIView):
    throttle_scope = "auth_refresh"

    def post(self, request):
        raw = request.data.get("refresh")
        if not raw:
            raise InvalidToken("refresh is required.")
        try:
            preview = RefreshToken(raw)
            user = User.objects.filter(pk=preview[jwt_settings.USER_ID_CLAIM]).first()
            if user is None or not user.is_active:
                raise InvalidToken("This account cannot sign in.")
            serializer = TokenRefreshSerializer(data=request.data)
            serializer.is_valid(raise_exception=True)
        except TokenError as exc:
            raise InvalidToken(str(exc)) from exc
        return Response(serializer.validated_data, status=status.HTTP_200_OK)
