"""
NovelBridge — Adapter surface and integrity unit test.
Ensures all adapters adhere to the required surface and index consistency.
"""
from scripts.check_adapters import validate_adapters


def test_all_adapters_surface_and_index():
    total, errors = validate_adapters()
    assert total > 0, "Expected at least one adapter to be discovered"
    assert not errors, f"Adapter validation failed with errors: {errors}"
