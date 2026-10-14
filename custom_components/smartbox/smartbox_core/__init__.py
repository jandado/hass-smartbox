"""Core layer - domain state.

The versioned state store, pending-write confirmation,
availability-window logic and the reconnection policy, as HA-agnostic
asyncio driven by typed protocol events
(smartbox_protocol.Event), exposing typed change notifications. Zero
``homeassistant`` imports (enforced by tests/test_layering.py);
it must stay testable against the fake transport without the HA test
harness.
"""
