# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 João Tonini
"""NØMAÐ Alert System - Detection, Storage, and Dispatch."""

from .backends import EmailBackend, SlackBackend, WebhookBackend
from .dispatcher import AlertDispatcher, get_dispatcher, init_dispatcher, send_alert
from .thresholds import (
    DEFAULT_THRESHOLDS,
    ThresholdChecker,
    check_and_alert,
)

__all__ = [
    'AlertDispatcher',
    'send_alert',
    'init_dispatcher',
    'get_dispatcher',
    'EmailBackend',
    'SlackBackend',
    'WebhookBackend',
    'ThresholdChecker',
    'check_and_alert',
    'DEFAULT_THRESHOLDS',
]
