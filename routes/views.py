from rest_framework.response import Response
from rest_framework.views import APIView


class OptimizeRouteView(APIView):
    def post(self, request):
        return Response({"detail": "Not implemented yet."}, status=501)
