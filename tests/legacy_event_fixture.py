"""Explicit legacy Event-rendering fixture for historical protocol regressions.

These tests protect frozen Event responses, source bindings, expiry and old
renderers. Production EmberService hooks use raw slices; this subclass is never
imported by application code. New automatic recall tests use EmberService itself.
"""
from ember.service import EmberService


class LegacyEventService(EmberService):
    run_recall_pipeline = EmberService.legacy_event_recall_pipeline
