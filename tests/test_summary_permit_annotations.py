from __future__ import annotations
import typing
from bot.services.request_priority import _GatePermit, ReservedCapacityGate

def test_permit_annotations_are_resolvable():
    assert typing.get_type_hints(_GatePermit.__init__)["semaphores"] == list[typing.Any]
    assert typing.get_type_hints(ReservedCapacityGate.acquire_permit)["return"] is _GatePermit
