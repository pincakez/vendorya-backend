from django.urls import path, include
from rest_framework.routers import DefaultRouter

from .views import (
    AISettingsView, AIStatusView,
    AIProfileViewSet, AIModelCacheViewSet,
    AIConversationViewSet, AIChatView,
    AIKnowledgeChunkViewSet, AIToolListView,
)

router = DefaultRouter()
router.register(r'profiles',      AIProfileViewSet,        basename='ai-profile')
router.register(r'models',        AIModelCacheViewSet,     basename='ai-model')
router.register(r'conversations', AIConversationViewSet,   basename='ai-conversation')
router.register(r'kb',            AIKnowledgeChunkViewSet, basename='ai-kb')

urlpatterns = [
    path('settings/', AISettingsView.as_view(), name='ai-settings'),
    path('status/',   AIStatusView.as_view(),   name='ai-status'),
    path('chat/',     AIChatView.as_view(),     name='ai-chat'),
    path('tools/',    AIToolListView.as_view(), name='ai-tools'),
    path('', include(router.urls)),
]
