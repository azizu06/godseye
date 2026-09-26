"""Logging-only drive adapter. No network, serial, or hardware access."""
import logging

logger = logging.getLogger(__name__)


def drive(v_mps: float, yaw_rate_rps: float) -> None:
    logger.info('DRIVE STUB v_mps=%s yaw_rate_rps=%s (not transmitted)', v_mps, yaw_rate_rps)
