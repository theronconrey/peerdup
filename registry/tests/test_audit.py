"""Tests for registry/registry/audit.py."""

import json
import os

import pytest

from registry.audit import AuditLogger, _NoOpAuditLogger, make_audit_logger


class TestAuditLoggerWritesJsonLines:
    def test_writes_json_lines(self, tmp_path):
        log_file = str(tmp_path / "audit.log")
        logger = AuditLogger(log_file=log_file)

        logger.log("register_peer", "peer123", "ok")
        logger.log("announce", "peer456", "denied",
                   share_id="share789", remote_ip="1.2.3.4")

        with open(log_file) as f:
            lines = [l.strip() for l in f if l.strip()]

        assert len(lines) == 2
        rec1 = json.loads(lines[0])
        rec2 = json.loads(lines[1])
        assert rec1["action"] == "register_peer"
        assert rec2["action"] == "announce"


class TestAuditLoggerAllFields:
    def test_all_fields_present(self, tmp_path):
        log_file = str(tmp_path / "audit.log")
        logger = AuditLogger(log_file=log_file)

        logger.log("create_share", "peerId1", "ok",
                   share_id="shareId1", remote_ip="10.0.0.1")

        with open(log_file) as f:
            line = f.readline().strip()
        rec = json.loads(line)

        assert "ts" in rec
        assert rec["action"] == "create_share"
        assert rec["peer_id"] == "peerId1"
        assert rec["share_id"] == "shareId1"
        assert rec["remote_ip"] == "10.0.0.1"
        assert rec["outcome"] == "ok"
        # ts should be ISO 8601
        assert "T" in rec["ts"]
        assert "+00:00" in rec["ts"] or rec["ts"].endswith("Z")


class TestNoOpLoggerDoesNotWrite:
    def test_noop_does_not_create_file(self, tmp_path):
        log_file = str(tmp_path / "should_not_exist.log")
        logger = make_audit_logger({"enabled": False})

        # Calling log() should silently do nothing
        logger.log("announce", "peer1", "ok", share_id="share1", remote_ip="1.2.3.4")

        assert not os.path.exists(log_file)

    def test_noop_instance_type(self):
        logger = make_audit_logger({"enabled": False})
        assert isinstance(logger, _NoOpAuditLogger)


class TestAuditLoggerEnabledConfig:
    def test_enabled_config_creates_file_and_writes(self, tmp_path):
        log_file = str(tmp_path / "audit.log")
        logger = make_audit_logger({"enabled": True, "log_file": log_file})

        assert isinstance(logger, AuditLogger)
        logger.log("get_peer", "peer1", "ok")

        assert os.path.exists(log_file)
        with open(log_file) as f:
            line = f.readline().strip()
        rec = json.loads(line)
        assert rec["action"] == "get_peer"
        assert rec["peer_id"] == "peer1"


class TestOutcomeValues:
    def test_outcome_roundtrip(self, tmp_path):
        log_file = str(tmp_path / "audit.log")
        logger = AuditLogger(log_file=log_file)

        for outcome in ("ok", "denied", "error"):
            logger.log("some_action", "peer1", outcome)

        with open(log_file) as f:
            lines = [l.strip() for l in f if l.strip()]

        assert len(lines) == 3
        outcomes = [json.loads(l)["outcome"] for l in lines]
        assert outcomes == ["ok", "denied", "error"]
