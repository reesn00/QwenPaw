# -*- coding: utf-8 -*-
"""Tests for trajectory service registry."""

from __future__ import annotations

from pathlib import Path

import pytest

from qwenpaw.trajectory import (
    get_trajectory_service,
    register_trajectory_service,
    TrajectoryService,
    unregister_trajectory_service,
)
from qwenpaw.trajectory.models import TrajectoryConfig


@pytest.fixture
def service(tmp_path: Path):
    return TrajectoryService(
        tmp_path,
        config=TrajectoryConfig(enabled=True),
    )


def test_register_and_get(service: TrajectoryService):
    register_trajectory_service("agent-test", service)
    assert get_trajectory_service("agent-test") is service
    unregister_trajectory_service("agent-test")
    assert get_trajectory_service("agent-test") is None


def test_build_config_from_dict():
    from qwenpaw.trajectory.service import build_trajectory_config

    class FakeConfig:
        running = {"trajectory_config": {"enabled": False, "retention_days": 7}}

    cfg = build_trajectory_config(FakeConfig())
    assert cfg.enabled is False
    assert cfg.retention_days == 7
