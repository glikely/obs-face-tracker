"""Covers the tracker driving a real obs-ptz device, against obs-ptz's own
camera simulator, ptzsim, which is what the test device of
src/test-ptz-device.c only models.
Before this, a change to obs-ptz's API went unseen until a camera was used.

Needs obs-ptz built with version 0.7 of the PTZ API or later, its checkout, and
the face detector's model:
OBS_PTZ_PLUGIN is its obs-ptz.plugin, and OBS_PTZ_REPO the checkout that has
scripts/ptzsim; see conftest.py.
"""

import json
import os
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import pytest
from conftest import FACE_IMAGE, free_port, have_detector_model, output_log, wait_for_port

OBS_PTZ_REPO = os.environ.get("OBS_PTZ_REPO")
FILTER_KIND = "ca.secretlab.obs-ptz.visca"
TRACKER = {"scale": 1.0, "track_x": 0.0, "track_y": 0.0}

pytestmark = pytest.mark.skipif(
    not have_detector_model() or not FACE_IMAGE.is_file() or not os.environ.get("OBS_PTZ_PLUGIN")
    or not OBS_PTZ_REPO or not (Path(OBS_PTZ_REPO) / "scripts" / "ptzsim").is_dir(),
    reason="needs OBS_PTZ_PLUGIN and OBS_PTZ_REPO, FACE_TRACKER_DATA, and dlib's example faces")


class Sim:
    def __init__(self, tcp_port, debug_port):
        self.tcp_port = tcp_port
        self.debug_url = f"http://127.0.0.1:{debug_port}/state"

    def state(self):
        with urllib.request.urlopen(self.debug_url, timeout=5) as resp:
            return json.loads(resp.read())

    def wait_for_state(self, predicate, timeout=20):
        deadline = time.time() + timeout
        last = None
        while time.time() < deadline:
            last = self.state()
            if predicate(last):
                return last
            time.sleep(0.1)
        raise AssertionError(f"the camera never was as expected; last seen: {last}")

    def visca(self, *payload):
        """Sends a VISCA command to the camera, as another controller would"""
        with socket.create_connection(("127.0.0.1", self.tcp_port), timeout=5) as s:
            s.sendall(bytes(payload))
            s.settimeout(1)
            try:
                s.recv(64)
            except socket.timeout:
                pass


@pytest.fixture(scope="module")
def sim():
    tcp_port, debug_port = free_port(), free_port()
    cmd = [sys.executable, "-m", "ptzsim", "--host", "127.0.0.1", "--visca-tcp-port", str(tcp_port),
           "--debug-http-port", str(debug_port), "--no-visca-serial", "--no-pelco"]
    with output_log("ptzsim") as out:
        proc = subprocess.Popen(cmd, cwd=Path(OBS_PTZ_REPO) / "scripts", stdout=out, stderr=subprocess.STDOUT)
    try:
        wait_for_port("127.0.0.1", debug_port, timeout=15)
        yield Sim(tcp_port, debug_port)
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


class Camera:
    """An obs-ptz device for the simulator, found by its UUID"""

    def __init__(self, world, sim):
        self.source = world.unique("ptz-camera")
        self.host, self.port = "127.0.0.1", sim.tcp_port
        world.create_input("face_tracker_test_video_source", self.source)
        self.name = world.unique("PTZ")
        world.ws.call("CreateSourceFilter", {
            "sourceName": self.source, "filterName": self.name, "filterKind": FILTER_KIND,
            "filterSettings": {"type": "visca-over-tcp", "host": "127.0.0.1", "tcp_port": sim.tcp_port},
        })
        # Any test device lists them, when it is given a file to list to
        helper = world.make_device()
        self.uuid = None
        for attempt in range(50):
            listing = world.tmp / f"{self.name}-{attempt}.list"
            helper.configure(list_file=str(listing))
            for _ in range(20):
                if listing.exists():
                    break
                time.sleep(0.1)
            for line in listing.read_text().splitlines() if listing.exists() else []:
                name, uuid, version = line.split("\t")
                if name == self.name:
                    self.uuid, self.version = uuid, version
            if self.uuid:
                break
            time.sleep(0.2)
        assert self.uuid, f"obs-ptz has no device named {self.name}"


@pytest.fixture
def camera(world, sim):
    return Camera(world, sim)


def follow(world, camera, center, **settings):
    source = world.make_face_source(center)
    return source, world.make_tracker(source, **{**TRACKER, "ptz-device": camera.uuid, **settings})


def test_obs_ptz_has_the_version_the_tracker_takes(camera):
    major, minor = camera.version.split(".")
    # 0.7 is the first whose ptz_get_state any thread may call
    assert int(major) == 0 and int(minor) >= 7


def test_the_camera_pans_towards_a_face_on_the_right(world, sim, camera):
    _, tracker = follow(world, camera, (0.75, 0.5))
    sim.wait_for_state(lambda s: s["pan_speed"] > 0)
    tracker.remove()
    sim.wait_for_state(lambda s: s["pan_speed"] == 0 and s["tilt_speed"] == 0 and s["zoom_speed"] == 0)


def test_the_camera_pans_towards_a_face_on_the_left(world, sim, camera):
    _, tracker = follow(world, camera, (0.25, 0.5))
    sim.wait_for_state(lambda s: s["pan_speed"] < 0)
    tracker.remove()
    sim.wait_for_state(lambda s: s["pan_speed"] == 0)


def test_the_camera_tilts_towards_a_face_above(world, sim, camera):
    _, tracker = follow(world, camera, (0.5, 0.25))
    sim.wait_for_state(lambda s: s["tilt_speed"] > 0)
    tracker.remove()
    sim.wait_for_state(lambda s: s["tilt_speed"] == 0)


def test_the_camera_tilts_towards_a_face_below(world, sim, camera):
    _, tracker = follow(world, camera, (0.5, 0.75))
    sim.wait_for_state(lambda s: s["tilt_speed"] < 0)
    tracker.remove()
    sim.wait_for_state(lambda s: s["tilt_speed"] == 0)


def test_the_camera_zooms_in_for_a_face_that_is_too_small(world, sim, camera):
    _, tracker = follow(world, camera, (0.5, 0.5), track_z=2.0)
    sim.wait_for_state(lambda s: s["zoom_speed"] > 0)
    tracker.remove()
    sim.wait_for_state(lambda s: s["zoom_speed"] == 0)


def test_the_camera_zooms_out_for_a_face_that_is_too_large(world, sim, camera):
    _, tracker = follow(world, camera, (0.5, 0.5), track_z=0.1)
    sim.wait_for_state(lambda s: s["zoom_speed"] < 0)
    tracker.remove()
    sim.wait_for_state(lambda s: s["zoom_speed"] == 0)


def test_the_camera_goes_to_the_preset_when_the_face_is_lost(world, sim, camera):
    # Where the camera is when it is saved as slot 3: moved there by following a face
    _, tracker = follow(world, camera, (0.9, 0.5))
    saved = sim.wait_for_state(lambda s: s["pan"] > 0.02)
    tracker.remove()
    saved = sim.wait_for_state(lambda s: s["pan_speed"] == 0)
    sim.visca(0x81, 0x01, 0x04, 0x3F, 0x01, 0x03, 0xFF)
    sim.wait_for_state(lambda s: len(s["presets"]) > 0)

    # Away from it, by following a face on the other side
    source, tracker = follow(world, camera, (0.1, 0.5), face_lost_ptz_preset=3, face_lost_preset_timeout=1.0)
    sim.wait_for_state(lambda s: s["pan"] < saved["pan"] - 0.02)

    # Then the face is lost, and the camera goes back to where it was saved
    world.move_face(source, (5.0, 5.0))
    sim.wait_for_state(lambda s: abs(s["pan"] - saved["pan"]) < 0.01 and s["pan_speed"] == 0, timeout=30)
    tracker.remove()


def test_the_tracker_learns_where_a_camera_is_zoomed_to_when_it_finds_it(world, sim):
    """A device says what changed, and one at rest says nothing, so the tracker
    that finds a camera that was zoomed in has to ask it where it is, or it
    takes the camera for one that is not zoomed, and scales its gain by that."""
    import re
    import time

    # Zoom direct, to 0x4000: a good way in, whatever range a driver takes it in
    sim.visca(0x81, 0x01, 0x04, 0x47, 0x04, 0x00, 0x00, 0x00, 0xFF)
    assert sim.state()["zoom"] > 0.2

    camera = Camera(world, sim)
    time.sleep(3)  # it has polled the camera, and said where it is, to nobody
    world.make_tracker(camera.source, **{"ptz-device": camera.uuid, "Kp_x_db": -40.0, "Kp_y_db": -40.0,
                                          "Kp_z_db": -40.0})

    def told():
        for line in world.log_lines():
            m = re.search(r"'" + re.escape(camera.name) + r"' is at pan (\S+) tilt (\S+) zoom (\S+)", line)
            if m:
                return float(m.group(3))
        return None

    world.wait_for(lambda: told() is not None, timeout=20)
    # as obs-ptz normalizes it, and not as the simulator does
    assert told() > 0.2
