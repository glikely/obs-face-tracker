"""Covers what the tracker asks of its PTZ device when it sees a face, which
is the control loop of src/face-tracker-ptz.cpp and how its speeds reach the
device in src/ptz-controller.cpp: the direction of each axis.

The face is a photo shown by the test video source
(tests/test-sources/test-video-source.c), placed away from where the tracker
is told to hold it, and the device's log says which way it was asked to move.
Needs the face detector's model: see FACE_TRACKER_DATA in conftest.py.
"""

import pytest
from conftest import FACE_IMAGE, have_detector_model

pytestmark = pytest.mark.skipif(
    not have_detector_model() or not FACE_IMAGE.is_file(),
    reason="needs FACE_TRACKER_DATA with the face detector model, and dlib's example faces (submodules)")

# The image is large enough for the detector at full scale
TRACKER = {"scale": 1.0, "track_x": 0.0, "track_y": 0.0}


def moves(device):
    """(pan, tilt, zoom) of each move the device was asked for"""
    return [tuple(float(v) for v in line.split()[1:]) for line in device.calls("move")]


def drive(world, center, **settings):
    """Shows a face at center and returns the moves the tracker made, once it
    has made some."""
    device = world.make_device()
    source = world.make_face_source(center)
    tracker = world.make_tracker(source, **{**TRACKER, "ptz-device": device.uuid, **settings})
    world.wait_for(lambda: moves(device), timeout=20)
    made = moves(device)
    tracker.remove()
    return made


def test_a_face_to_the_right_is_followed_to_the_right(world):
    assert drive(world, (0.75, 0.5))[0][0] > 0


def test_a_face_to_the_left_is_followed_to_the_left(world):
    assert drive(world, (0.25, 0.5))[0][0] < 0


def test_a_face_above_is_followed_up(world):
    assert drive(world, (0.5, 0.25))[0][1] > 0


def test_a_face_below_is_followed_down(world):
    assert drive(world, (0.5, 0.75))[0][1] < 0
