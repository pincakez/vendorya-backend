from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from rest_framework_simplejwt.views import TokenObtainPairView
from .serializers import VendoryaTokenObtainSerializer, UserProfileSerializer


class VendoryaTokenObtainView(TokenObtainPairView):
    serializer_class = VendoryaTokenObtainSerializer


class MeView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        return Response(UserProfileSerializer(request.user).data)
