"""Covers how face_tracker_ptz finds and drives a PTZ device of the PTZ device
API (src/ptz-controller.cpp), against the test device of
tests/test-sources/test-ptz-device.c.

A tracker with no face in view moves nothing, so what can be seen is what it
does about its device: finding it by UUID, checking its API version, and
stopping it.
"""

import time

import pytest

# How long to wait to be sure that something does not happen
QUIET = 2


def test_a_tracker_with_no_device_does_nothing(world):
    device = world.make_device()
    tracker = world.make_tracker()
    time.sleep(QUIET)
    tracker.remove()
    time.sleep(QUIET)
    # The device may be asked its version, by another plugin or when the
    # tracker looks for the device to use, but is not driven
    assert [line for line in device.log() if line != "version_query"] == []


def test_the_tracker_checks_the_device_it_is_given(world):
    device = world.make_device()
    tracker = world.track_device(device)
    tracker.remove()


def test_the_tracker_stops_its_device_when_it_goes(world):
    device = world.make_device()
    tracker = world.track_device(device)

    tracker.remove()
    device.wait_for_call("stop")


def test_an_idle_tracker_does_not_move_the_device(world):
    device = world.make_device()
    tracker = world.track_device(device)
    time.sleep(QUIET)
    assert device.calls("move") == []
    assert device.calls("stop") == []
    tracker.remove()


def test_the_tracker_stops_the_device_it_leaves(world):
    first = world.make_device()
    second = world.make_device()
    tracker = world.track_device(first)

    tracker.configure(**{"ptz-device": second.uuid})
    first.wait_for_call("stop")
    assert second.calls("stop") == []
    tracker.remove()


def test_the_tracker_takes_a_device_with_none_selected(world):
    device = world.make_device()
    tracker = world.track_device(device)

    tracker.configure(**{"ptz-device": ""})
    device.wait_for_call("stop")
    tracker.remove()


@pytest.mark.parametrize("version", [{"api_major": 1, "api_minor": 0}, {"api_major": 0, "api_minor": 6}],
                         ids=["newer major", "older minor"])
def test_the_tracker_leaves_a_device_of_another_api_version(world, version):
    device = world.make_device(**version)
    tracker = world.track_device(device)

    tracker.remove()
    time.sleep(QUIET)
    # asked, and not driven
    assert device.calls("stop") == []
    assert device.calls("move") == []


def test_a_newer_minor_version_is_taken(world):
    device = world.make_device(api_minor=8)
    tracker = world.track_device(device)

    tracker.remove()
    device.wait_for_call("stop")


def test_a_source_that_is_not_a_device_is_left_alone(world):
    other = world.create_input("face_tracker_test_video_source", world.unique("not-a-device"))
    tracker = world.make_tracker(**{"ptz-device": other})
    time.sleep(QUIET)
    tracker.remove()
    world.ws.call("GetVersion")


def test_a_device_that_is_gone_is_survived(world):
    device = world.make_device()
    tracker = world.track_device(device)

    device.remove()
    time.sleep(QUIET)
    tracker.remove()
    world.ws.call("GetVersion")


def test_a_device_that_is_not_found_is_survived(world):
    # A UUID that no source has, as when the tracker is loaded before its
    # device is
    tracker = world.make_tracker(**{"ptz-device": "00000000-0000-4000-8000-000000000000"})
    time.sleep(QUIET)
    tracker.remove()
    world.ws.call("GetVersion")


def test_the_device_may_announce_its_zoom(world):
    device = world.make_device()
    tracker = world.track_device(device)

    for zoom in (0.25, 0.5, 1.0, 0.0):
        device.configure(zoom=zoom)
    time.sleep(1)
    tracker.remove()
    device.wait_for_call("stop")
