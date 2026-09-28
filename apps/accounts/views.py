from __future__ import annotations

from django.core.exceptions import ValidationError
from rest_framework import generics, permissions, status
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.accounts import services
from apps.accounts.exceptions import DomainError
from apps.accounts.models import User
from apps.accounts.serializers import (
    AuthResponseSerializer,
    EVMWalletProofSerializer,
    TelegramMiniAppAuthSerializer,
    TelegramWidgetAuthSerializer,
    TonWalletProofSerializer,
    UserProfileUpdateSerializer,
    UserSerializer,
    WalletChallengeRequestSerializer,
    WalletChallengeSerializer,
)


class AuthChallengeView(APIView):
    permission_classes = [permissions.AllowAny]

    def post(self, request):
        serializer = WalletChallengeRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        challenge = services.create_wallet_challenge(
            chain=serializer.validated_data["chain"],
            address=serializer.validated_data.get("address"),
        )
        return Response(WalletChallengeSerializer(challenge).data)


class LoginTelegramMiniAppView(APIView):
    permission_classes = [permissions.AllowAny]

    def post(self, request):
        serializer = TelegramMiniAppAuthSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            result = services.login_with_telegram_mini_app(init_data=serializer.validated_data["init_data"])
        except DomainError as exc:
            return Response({"detail": exc.message, "code": exc.code}, status=status.HTTP_400_BAD_REQUEST)
        return Response(AuthResponseSerializer(result).data)


class LoginTelegramWidgetView(APIView):
    permission_classes = [permissions.AllowAny]

    def post(self, request):
        serializer = TelegramWidgetAuthSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            result = services.login_with_telegram_widget(auth_data=serializer.validated_data["auth_data"])
        except DomainError as exc:
            return Response({"detail": exc.message, "code": exc.code}, status=status.HTTP_400_BAD_REQUEST)
        return Response(AuthResponseSerializer(result).data)


class LoginEVMView(APIView):
    permission_classes = [permissions.AllowAny]

    def post(self, request):
        serializer = EVMWalletProofSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        try:
            result = services.login_with_wallet(
                chain="evm",
                address=data["address"],
                signature=data["signature"],
                nonce=data["nonce"],
            )
        except DomainError as exc:
            return Response({"detail": exc.message, "code": exc.code}, status=status.HTTP_400_BAD_REQUEST)
        return Response(AuthResponseSerializer(result).data)


class LoginTONView(APIView):
    permission_classes = [permissions.AllowAny]

    def post(self, request):
        serializer = TonWalletProofSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        proof = serializer.to_proof()
        try:
            result = services.login_with_ton_proof(proof=proof)
        except DomainError as exc:
            return Response({"detail": exc.message, "code": exc.code}, status=status.HTTP_400_BAD_REQUEST)
        return Response(AuthResponseSerializer(result).data)


class WalletLinkView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        serializer = EVMWalletProofSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        try:
            user = services.link_wallet_to_user(
                user=request.user,
                chain="evm",
                address=data["address"],
                signature=data["signature"],
                nonce=data["nonce"],
            )
        except DomainError as exc:
            return Response({"detail": exc.message, "code": exc.code}, status=status.HTTP_400_BAD_REQUEST)
        return Response(UserSerializer(user).data)


class ProfileView(generics.RetrieveUpdateAPIView):
    serializer_class = UserSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_object(self):
        return self.request.user

    def update(self, request, *args, **kwargs):
        partial = True
        serializer = UserProfileUpdateSerializer(instance=self.get_object(), data=request.data, partial=partial)
        serializer.is_valid(raise_exception=True)
        user = serializer.save()
        return Response(UserSerializer(user).data)
