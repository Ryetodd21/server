"""
Firebase Cloud Messaging (FCM) service — Round 27.

Sends push notifications to Android devices via FCM HTTP v1 API. The
service is **graceful-fallback** by design: if the FCM service account
JSON is not present, all `send_fcm_notification()` calls log a warning
and return False without raising. This lets the rest of the app
function normally (cluster transitions, daily summaries, etc.) while
the user sets up their Firebase project.

Setup steps (one-time, requires user action):

  1. Create a Firebase project at https://console.firebase.google.com
  2. Add an Android app with package `com.monitorung.app`
  3. Download `google-services.json` and place it in
     `monitor-ung-app/app/google-services.json`
  4. Generate a service account key (Project Settings → Service
     Accounts → Generate new private key) and save it as
     `/home/terio-op/secrets/fcm-service-account.json`
  5. Set `FCM_SERVICE_ACCOUNT_PATH=/home/terio-op/secrets/fcm-service-account.json`
     in `.env.production`
  6. Rebuild the api container — startup will detect the credentials
     and initialize the Firebase Admin SDK

The Android side mirrors this: `FirebaseMessagingService` is wired up
in code but stays dormant until `google-services.json` is present.

This module's logic is split into two layers:
  - This file: the public API and the FCM admin SDK wrapper
  - The migration / model that stores FCM tokens per user
"""
from __future__ import annotations

import logging
import os
from typing import Any, Mapping

logger = logging.getLogger("fcm")

# These are populated by `init_fcm()` if credentials are present.
_fcm_enabled: bool = False
_firebase_app = None  # firebase_admin.App instance (or None)


def init_fcm() -> None:
    """Initialize the FCM admin SDK at app startup.

    Called from `app/main.py` lifespan. If no service account is
    configured, the function logs once at WARNING level and returns.
    All subsequent `send_fcm_notification()` calls become no-ops.
    """
    global _fcm_enabled, _firebase_app

    sa_path = os.environ.get("FCM_SERVICE_ACCOUNT_PATH", "").strip()
    if not sa_path:
        logger.warning(
            "FCM not configured: FCM_SERVICE_ACCOUNT_PATH env var is empty. "
            "Push notifications are disabled. See app/services/fcm.py for setup."
        )
        return

    if not os.path.isfile(sa_path):
        logger.warning(
            "FCM not configured: %s does not exist. "
            "Push notifications are disabled.", sa_path
        )
        return

    try:
        import firebase_admin
        from firebase_admin import credentials, messaging

        cred = credentials.Certificate(sa_path)
        # Avoid double-init if FastAPI reloads the lifespan
        try:
            _firebase_app = firebase_admin.get_app()
        except ValueError:
            _firebase_app = firebase_admin.initialize_app(cred)

        _fcm_enabled = True
        logger.info("FCM initialized: project_id=%s", cred.project_id)
    except Exception:
        logger.exception("FCM init failed — push notifications disabled")
        _fcm_enabled = False
        _firebase_app = None


def is_enabled() -> bool:
    """Whether FCM is currently active. Use this to short-circuit call
    sites that want to avoid even the cost of building the payload."""
    return _fcm_enabled


async def send_fcm_notification(
    device_token: str,
    title: str,
    body: str,
    data: Mapping[str, str] | None = None,
) -> bool:
    """Send a single FCM notification to a device.

    Returns True on success, False on any failure (including FCM not
    configured, invalid token, network error). Errors are logged at
    WARNING level — callers should treat the return value as a soft
    success indicator, not a hard precondition.

    The `data` dict is included as FCM data fields (string keys and
    values only — FCM rejects non-string types). Clients receive them
    via `FirebaseMessagingService.onMessageReceived()` as a Bundle.
    """
    if not _fcm_enabled:
        logger.debug("FCM disabled, skipping push to token (truncated): %s", device_token[:12])
        return False
    if not device_token:
        return False

    try:
        from firebase_admin import messaging

        # data values must be strings; coerce any non-string values
        sanitized_data: dict[str, str] = {}
        if data:
            for k, v in data.items():
                sanitized_data[k] = str(v) if v is not None else ""

        message = messaging.Message(
            notification=messaging.Notification(title=title, body=body),
            data=sanitized_data,
            token=device_token,
        )
        message_id = messaging.send(message)
        logger.info("FCM sent: %s to token %s...", message_id, device_token[:12])
        return True
    except Exception:
        logger.exception("FCM send failed for token %s...", device_token[:12])
        return False


async def send_to_dosen_for_cluster_transition(
    fcm_tokens: list[str],
    student_name: str,
    new_cluster: str,
) -> int:
    """Convenience: notify a dosen about a student's cluster transition.

    Called by the cluster pipeline when a student moves into a higher
    distraction cluster. Sends to each FCM token in the list and
    returns the number of successful sends.

    No-op if FCM is disabled or `fcm_tokens` is empty.
    """
    if not _fcm_enabled or not fcm_tokens:
        return 0
    title = "Mahasiswa butuh intervensi"
    body = f"{student_name} berpindah ke klaster {new_cluster}. Mohon tinjau dasbor."
    success = 0
    for token in fcm_tokens:
        ok = await send_fcm_notification(
            token,
            title,
            body,
            data={
                "event": "cluster_transition",
                "student_name": student_name,
                "cluster": new_cluster,
                "deep_link": "/students",
            },
        )
        if ok:
            success += 1
    return success
