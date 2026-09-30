"""Tests for the soft-close pin-precondition contract (issue #116).

A soft-closed thread still accepts comments, but only from callers who
acknowledge they have read the current thread head.  The gate lives in
POST /api/posts/{post_id}/comments and enforces three outcomes:

  428  SOFT_CLOSE_ACKNOWLEDGMENT_REQUIRED — header absent
  409  SOFT_CLOSE_PIN_MISMATCH             — header present but stale
  201  (n