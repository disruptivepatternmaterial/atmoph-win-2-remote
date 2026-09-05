"""What a real window does, shared by both suites.

The two suites keep separate fakes because only one of them may import Home
Assistant, but the device behaviour they model is one device. Everything here
is a fact about the hardware recorded in `docs/PROTOCOL.md`, so it lives in
one place rather than drifting between two copies. It imports nothing, which
is what keeps it usable from the Home Assistant free suite.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from types import ModuleType

# The bounds one reported device gave. They are per-device and wider than a
# ten-step slider suggests, so a fake reporting a narrow range would let a
# hardcoded one pass. Every key the app knows is present: a fake that omits
# one leaves the entity behind it registered and permanently unavailable,
# which is indistinguishable from a broken entity.
REPORTED_SETTINGS: dict[str, object] = {
    "ScreenBrightness": {"min": 1, "max": 25, "value": 6},
    "LandscapeVolumeLevel": {"min": 0, "max": 24, "value": 12},
    "SoundscapeVolumeLevel": {"min": 0, "max": 20, "value": 8},
    "LedBrightness": {"min": 0, "max": 20, "value": 4},
    "CurrentDecoration": {"min": 0, "max": 19, "value": 3},
    "SoundscapeLayer": {"min": 0, "max": 5, "value": 2},
    "WidgetsVisible": True,
    "DailyRoutineEnable": False,
    "SoundOnly": False,
}

# A toggle sent within roughly a second of one that took effect is discarded,
# with no ATT error and no state change.
TOGGLE_DROP_WINDOW = 1.0


class FakeCharacteristic:
    """The object bleak hands to a notification callback.

    Bleak passes the characteristic the notification came from, never its
    UUID, so a fake that passes a bare string would let the client read
    `sender` directly and still pass every test.
    """

    def __init__(self, uuid: str) -> None:
        self.uuid = uuid

    def __str__(self) -> str:
        # Bleak's own string form is a description, not the UUID, so a client
        # that stringifies the characteristic has to fail here too.
        return f"<FakeCharacteristic at {id(self):#x}>"


class FakeClock:
    """A virtual clock that advances only when the client waits.

    The delays are part of what is under test - the pause before a retry has
    to outlast the window in which the display ignores a toggle - so they are
    recorded rather than collapsed to nothing.
    """

    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    async def sleep(self, delay: float) -> None:
        """Advance the clock instead of waiting."""
        self.sleeps.append(delay)
        self.now += delay

    @property
    def last_sleep(self) -> float:
        """Return how long the client last waited before acting."""
        return self.sleeps[-1] if self.sleeps else 0.0

    def patched_asyncio(self) -> ModuleType:
        """Return an `asyncio` whose `sleep` is this clock's, for the client only.

        Patching `asyncio.sleep` itself would replace it for the whole
        process, including the yields Home Assistant's test harness makes to
        let the event loop run - so a notification would be scheduled and
        never delivered, and every push test would fail for the wrong reason.
        Substituting the module in one namespace keeps the blast radius to the
        module under test.
        """
        shim = ModuleType(asyncio.__name__)
        shim.__dict__.update(asyncio.__dict__)
        shim.sleep = self.sleep
        return shim


@dataclass(frozen=True, slots=True)
class Toggle:
    """One `S` write, and whether the display was still ignoring toggles."""

    at: float
    pause: float
    accepted: bool


class DisplayPower:
    """The display's half of the toggle protocol.

    Kept apart from the transport because both fakes need it and neither has
    anywhere sensible to put it: it is the one piece of window behaviour with
    a memory of its own.
    """

    def __init__(self, clock: FakeClock, last_toggle_at: float | None = None) -> None:
        self.clock = clock
        self.toggles: list[Toggle] = []
        # None stands for a display nobody has touched recently, so the next
        # toggle lands outside the window in which one is discarded.
        self._accepted_at = last_toggle_at

    def touch(self) -> None:
        """Record a toggle that took effect just now, from the panel or the app."""
        self._accepted_at = self.clock.now

    def toggle(self) -> bool:
        """Return whether an `S` write took effect."""
        accepted = (
            self._accepted_at is None
            or self.clock.now - self._accepted_at > TOGGLE_DROP_WINDOW
        )
        self.toggles.append(Toggle(self.clock.now, self.clock.last_sleep, accepted))
        if accepted:
            self._accepted_at = self.clock.now
        return accepted
