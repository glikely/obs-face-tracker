"""Covers the tracker choosing a PTZ device for itself when it has none: for a
scene saved by a version that had a PTZ type and its address or device id
(migrate_ptz_device() in src/face-tracker-ptz.cpp), and for one that has none
chosen at all.
What it chooses is written to its settings, which is where the test looks.
"""

import time

from conftest import TRACKER_KIND

# How long to wait to be sure that nothing is chosen
QUIET = 4


def chosen(world, tracker, timeout=10):
    """The device a tracker has chosen for itself"""
    world.wait_for(lambda: tracker.settings().get("ptz-device"), timeout=timeout)
    return tracker.settings()["ptz-device"]


def not_chosen(tracker):
    time.sleep(QUIET)
    return "ptz-device" not in tracker.settings()


def visca_device(world, host, port):
    """A device that says it is VISCA over TCP to an address"""
    return world.make_device(type="visca-over-tcp", host=host, tcp_port=port)


def test_the_address_and_port_of_the_old_type_find_the_device(world):
    other = visca_device(world, "192.0.2.10", 5678)
    wanted = visca_device(world, "192.0.2.20", 5678)
    tracker = world.make_tracker(**{"ptz-type": "visca-over-tcp", "ptz.visca-over-tcp.address": "192.0.2.20",
                                    "ptz.visca-over-tcp.port": 5678})
    assert chosen(world, tracker) == wanted.uuid != other.uuid


def test_the_names_of_the_earliest_version_are_read(world):
    visca_device(world, "192.0.2.10", 1259)
    wanted = visca_device(world, "192.0.2.20", 1259)
    tracker = world.make_tracker(**{"ptz-type": "visca-over-tcp", "ptz-viscaip-address": "192.0.2.20",
                                    "ptz-viscaip-port": 1259})
    assert chosen(world, tracker) == wanted.uuid


def test_the_port_tells_two_devices_of_an_address_apart(world):
    visca_device(world, "192.0.2.30", 5678)
    wanted = visca_device(world, "192.0.2.30", 1259)
    tracker = world.make_tracker(**{"ptz-type": "visca-over-tcp", "ptz.visca-over-tcp.address": "192.0.2.30",
                                    "ptz.visca-over-tcp.port": 1259})
    assert chosen(world, tracker) == wanted.uuid


def test_the_device_on_the_source_of_the_tracker_is_chosen(world):
    other = world.make_device()
    wanted = world.make_device()
    # A device that is a source is its own, so the tracker is a filter of it
    tracker = world.make_tracker(wanted.name, **{"ptz-type": "obsptz", "ptz.obsptz.device_id": 3})
    assert chosen(world, tracker) == wanted.uuid != other.uuid


def test_a_new_tracker_takes_the_device_on_its_source(world):
    world.make_device()
    wanted = world.make_device()
    tracker = world.make_tracker(wanted.name)
    assert chosen(world, tracker) == wanted.uuid


def test_the_only_device_is_chosen_for_an_old_type(world):
    wanted = world.make_device()
    tracker = world.make_tracker(**{"ptz-type": "obsptz", "ptz.obsptz.device_id": 5})
    assert chosen(world, tracker) == wanted.uuid


def test_a_new_tracker_does_not_take_a_device_that_is_not_on_its_source(world):
    world.make_device()
    tracker = world.make_tracker()
    assert not_chosen(tracker)


def test_two_devices_are_not_told_apart_by_a_device_id(world):
    world.make_device()
    world.make_device()
    tracker = world.make_tracker(**{"ptz-type": "obsptz", "ptz.obsptz.device_id": 2})
    assert not_chosen(tracker)


def test_nothing_fits_an_address_that_no_device_has(world):
    visca_device(world, "192.0.2.10", 5678)
    visca_device(world, "192.0.2.11", 5678)
    tracker = world.make_tracker(**{"ptz-type": "visca-over-tcp", "ptz.visca-over-tcp.address": "192.0.2.99",
                                    "ptz.visca-over-tcp.port": 5678})
    assert not_chosen(tracker)


def test_none_is_kept(world):
    device = world.make_device()
    tracker = world.make_tracker(device.name, **{"ptz-device": ""})
    time.sleep(QUIET)
    assert tracker.settings()["ptz-device"] == ""


def test_a_chosen_device_is_kept(world):
    device = world.make_device()
    other = world.make_device()
    tracker = world.make_tracker(device.name, **{"ptz-device": other.uuid})
    time.sleep(QUIET)
    assert tracker.settings()["ptz-device"] == other.uuid


def test_the_type_of_none_is_not_a_device_to_find(world):
    world.make_device()
    tracker = world.make_tracker(**{"ptz-type": "dummy"})
    assert not_chosen(tracker)


def test_a_device_that_comes_later_is_chosen(world):
    # As when the camera's device is made after the tracker is, loading a scene
    tracker = world.make_tracker(**{"ptz-type": "visca-over-tcp", "ptz.visca-over-tcp.address": "192.0.2.40",
                                    "ptz.visca-over-tcp.port": 5678})
    time.sleep(1.5)
    assert "ptz-device" not in tracker.settings()

    wanted = visca_device(world, "192.0.2.40", 5678)
    assert chosen(world, tracker) == wanted.uuid
