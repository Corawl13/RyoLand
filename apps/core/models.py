"""Shared base models for the project."""
import uuid

from django.db import models


def generate_uuid() -> uuid.UUID:
    return uuid.uuid4()


class UUIDModel(models.Model):
    id = models.UUIDField(primary_key=True, default=generate_uuid, editable=False)

    class Meta:
        abstract = True


class TimeStampedModel(models.Model):
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True


class BaseModel(UUIDModel, TimeStampedModel):
    class Meta:
        abstract = True
