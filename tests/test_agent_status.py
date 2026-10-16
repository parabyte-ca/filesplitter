import json
import os
from datetime import datetime, timedelta, timezone

import updates as u


def _write_agent(upgrade_dir, last_seen, every_minutes):
    with open(os.path.join(upgrade_dir, "agent.json"), "w") as f:
        json.dump({"last_seen": last_seen, "every_minutes": every_minutes}, f)


def test_agent_status_not_configured(tmp_upgrade_dir):
    status = u.get_agent_status()
    assert status["configured"] is False
    assert status["connected"] is False


def test_agent_status_connected_recent(tmp_upgrade_dir):
    now = datetime.now(timezone.utc).isoformat()
    _write_agent(tmp_upgrade_dir, now, 15)
    status = u.get_agent_status()
    assert status["configured"] is True
    assert status["connected"] is True


def test_agent_status_connected_boundary(tmp_upgrade_dir):
    # threshold = max(3, 2*15+1) = 31 minutes
    just_inside = (datetime.now(timezone.utc) - timedelta(minutes=30, seconds=50)).isoformat()
    _write_agent(tmp_upgrade_dir, just_inside, 15)
    assert u.get_agent_status()["connected"] is True


def test_agent_status_disconnected_past_boundary(tmp_upgrade_dir):
    just_outside = (datetime.now(timezone.utc) - timedelta(minutes=32)).isoformat()
    _write_agent(tmp_upgrade_dir, just_outside, 15)
    assert u.get_agent_status()["connected"] is False


def test_agent_status_future_last_seen_beyond_skew_guard(tmp_upgrade_dir):
    future = (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat()
    _write_agent(tmp_upgrade_dir, future, 15)
    assert u.get_agent_status()["connected"] is False


def test_agent_status_future_last_seen_within_skew_guard(tmp_upgrade_dir):
    future = (datetime.now(timezone.utc) + timedelta(seconds=30)).isoformat()
    _write_agent(tmp_upgrade_dir, future, 15)
    assert u.get_agent_status()["connected"] is True
