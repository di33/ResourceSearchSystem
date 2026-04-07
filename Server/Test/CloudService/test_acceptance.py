"""Tests for acceptance checklist and delivery plan (Spec 12)."""

from __future__ import annotations

import pytest

from CloudService.acceptance import (
    AcceptanceCategory,
    AcceptanceChecklist,
    AcceptanceItem,
    build_default_checklist,
    build_delivery_plan,
)


# ---------------------------------------------------------------------------
# AcceptanceCategory
# ---------------------------------------------------------------------------

def test_acceptance_category_values():
    assert AcceptanceCategory.SECURITY == "security"
    assert AcceptanceCategory.FAULT_TOLERANCE == "fault_tolerance"
    assert AcceptanceCategory.PERFORMANCE == "performance"
    assert AcceptanceCategory.QUALITY == "quality"
    assert AcceptanceCategory.INTEGRATION == "integration"


# ---------------------------------------------------------------------------
# AcceptanceItem
# ---------------------------------------------------------------------------

def test_acceptance_item_defaults():
    item = AcceptanceItem(
        id="T-01",
        category=AcceptanceCategory.SECURITY,
        title="t",
        description="d",
        target="tgt",
        verification_method="vm",
    )
    assert item.passed is None
    assert item.notes == ""


# ---------------------------------------------------------------------------
# AcceptanceChecklist – basic operations
# ---------------------------------------------------------------------------

def _make_item(id_: str, cat: AcceptanceCategory, passed=None) -> AcceptanceItem:
    return AcceptanceItem(
        id=id_, category=cat, title="t", description="d",
        target="tgt", verification_method="vm", passed=passed,
    )


def test_checklist_add_and_count():
    cl = AcceptanceChecklist()
    assert len(cl.items) == 0
    cl.add(_make_item("A", AcceptanceCategory.SECURITY))
    cl.add(_make_item("B", AcceptanceCategory.QUALITY))
    assert len(cl.items) == 2


def test_checklist_pass_rate_all_passed():
    cl = AcceptanceChecklist()
    cl.add(_make_item("A", AcceptanceCategory.SECURITY, passed=True))
    cl.add(_make_item("B", AcceptanceCategory.SECURITY, passed=True))
    assert cl.pass_rate() == 1.0


def test_checklist_pass_rate_mixed():
    cl = AcceptanceChecklist()
    cl.add(_make_item("A", AcceptanceCategory.SECURITY, passed=True))
    cl.add(_make_item("B", AcceptanceCategory.SECURITY, passed=False))
    cl.add(_make_item("C", AcceptanceCategory.SECURITY, passed=None))
    assert cl.pass_rate() == pytest.approx(0.5)


def test_checklist_pass_rate_empty():
    cl = AcceptanceChecklist()
    assert cl.pass_rate() == 0.0


def test_checklist_by_category():
    cl = AcceptanceChecklist()
    cl.add(_make_item("A", AcceptanceCategory.SECURITY))
    cl.add(_make_item("B", AcceptanceCategory.QUALITY))
    cl.add(_make_item("C", AcceptanceCategory.SECURITY))
    sec = cl.by_category(AcceptanceCategory.SECURITY)
    assert len(sec) == 2
    assert all(i.category == AcceptanceCategory.SECURITY for i in sec)


def test_checklist_pending_items():
    cl = AcceptanceChecklist()
    cl.add(_make_item("A", AcceptanceCategory.SECURITY, passed=True))
    cl.add(_make_item("B", AcceptanceCategory.SECURITY, passed=None))
    cl.add(_make_item("C", AcceptanceCategory.SECURITY, passed=None))
    pending = cl.pending_items()
    assert len(pending) == 2
    assert all(i.passed is None for i in pending)


def test_checklist_failed_items():
    cl = AcceptanceChecklist()
    cl.add(_make_item("A", AcceptanceCategory.SECURITY, passed=True))
    cl.add(_make_item("B", AcceptanceCategory.SECURITY, passed=False))
    cl.add(_make_item("C", AcceptanceCategory.SECURITY, passed=False))
    failed = cl.failed_items()
    assert len(failed) == 2
    assert all(i.passed is False for i in failed)


def test_checklist_summary():
    cl = AcceptanceChecklist()
    cl.add(_make_item("A", AcceptanceCategory.SECURITY, passed=True))
    cl.add(_make_item("B", AcceptanceCategory.SECURITY, passed=False))
    cl.add(_make_item("C", AcceptanceCategory.SECURITY, passed=None))
    s = cl.summary()
    assert s["total"] == 3
    assert s["passed"] == 1
    assert s["failed"] == 1
    assert s["pending"] == 1
    assert s["pass_rate"] == pytest.approx(0.5)


# ---------------------------------------------------------------------------
# build_default_checklist
# ---------------------------------------------------------------------------

def test_build_default_checklist_completeness():
    cl = build_default_checklist()
    categories_present = {i.category for i in cl.items}
    for cat in AcceptanceCategory:
        assert cat in categories_present, f"{cat} missing from default checklist"


def test_default_checklist_has_security_items():
    cl = build_default_checklist()
    assert len(cl.by_category(AcceptanceCategory.SECURITY)) >= 4


def test_default_checklist_has_fault_tolerance_items():
    cl = build_default_checklist()
    assert len(cl.by_category(AcceptanceCategory.FAULT_TOLERANCE)) >= 5


def test_default_checklist_has_performance_items():
    cl = build_default_checklist()
    assert len(cl.by_category(AcceptanceCategory.PERFORMANCE)) >= 2


def test_default_checklist_has_quality_items():
    cl = build_default_checklist()
    assert len(cl.by_category(AcceptanceCategory.QUALITY)) >= 3


def test_default_checklist_has_integration_items():
    cl = build_default_checklist()
    assert len(cl.by_category(AcceptanceCategory.INTEGRATION)) >= 3


# ---------------------------------------------------------------------------
# build_delivery_plan
# ---------------------------------------------------------------------------

def test_delivery_plan_stages():
    plan = build_delivery_plan()
    assert len(plan) == 6
    for i, stage in enumerate(plan, start=1):
        assert stage.stage == i


def test_delivery_plan_specs_coverage():
    plan = build_delivery_plan()
    all_specs: set[str] = set()
    for stage in plan:
        all_specs.update(stage.specs)
    for n in range(1, 13):
        assert f"Spec {n}" in all_specs, f"Spec {n} not covered in delivery plan"
