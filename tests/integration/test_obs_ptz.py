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
from conftest import FACE_IMAGE, bounding_box, free_port, have_detector_model, is_marker, is_photo, output_log, wait_for_port

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


def test_a_tracker_of_an_old_scene_finds_the_device_on_its_source(world, camera):
    # As one saved with the old PTZ Controls type, on the camera's own source
    tracker = world.make_tracker(camera.source, **{"ptz-type": "obsptz", "ptz.obsptz.device_id": 2})
    world.wait_for(lambda: tracker.settings().get("ptz-device"), timeout=10)
    assert tracker.settings()["ptz-device"] == camera.uuid


def test_the_address_of_the_old_type_finds_the_device(world, camera):
    # On a source of its own, so it can only be found by the address
    tracker = world.make_tracker(**{"ptz-type": "visca-over-tcp", "ptz.visca-over-tcp.address": camera.host,
                                    "ptz.visca-over-tcp.port": camera.port})
    world.wait_for(lambda: tracker.settings().get("ptz-device"), timeout=10)
    assert tracker.settings()["ptz-device"] == camera.uuid


SOFT_KIND = "ca.secretlab.obs-ptz.soft-ptz"


def soft_ptz_on(world, center, is_there, **settings):
    """A Soft PTZ, which shows a part of the picture, and a tracker on the
    same source, which has the whole of it and finds the Soft PTZ by it being
    there. Waits for the viewport, as the Soft PTZ saves it once it has
    stopped, to be where is_there() says, and returns it."""
    source = world.make_face_source(center)
    soft = world.unique("Soft PTZ")
    world.ws.call("CreateSourceFilter", {"sourceName": source, "filterName": soft, "filterKind": SOFT_KIND,
                                         "filterSettings": {}})
    # Its default scale, which the rectangles of faces are not in the pixels of
    tracker = world.make_tracker(source, **{"track_x": 0.0, "track_y": 0.0, "track_z": 1.0, **settings})

    def viewport():
        saved = world.ws.call("GetSourceFilter", {"sourceName": source, "filterName": soft})["filterSettings"]
        return saved.get("viewport")

    # It does not stand still, as a dead zone has it move a little at a time,
    # so ask for where it is, and not for it to have stopped
    last = {}

    def there():
        last["pos"] = viewport()
        return last["pos"] is not None and is_there(last["pos"])

    try:
        world.wait_for(there, timeout=60, interval=1.0)
    except AssertionError:
        raise AssertionError(f"the viewport never got there; it was {last.get('pos')}")
    finally:
        tracker.remove()
    return last["pos"]


def test_a_digital_ptz_is_centered_on_a_face_on_the_right(world):
    # The viewport's centre is where the face is: pan is a unit of half the picture
    pos = soft_ptz_on(world, (0.65, 0.5), lambda p: p["zoom"] > 0.05 and abs(p["pan"] - 0.3) < 0.1)
    assert abs(pos["tilt"]) < 0.1


def test_a_digital_ptz_is_centered_on_a_face_on_the_left(world):
    soft_ptz_on(world, (0.35, 0.5), lambda p: p["zoom"] > 0.05 and abs(p["pan"] + 0.3) < 0.1)


def test_a_digital_ptz_is_centered_on_a_face_above(world):
    # Tilt is up, and the picture's y runs down
    pos = soft_ptz_on(world, (0.5, 0.35), lambda p: p["zoom"] > 0.05 and abs(p["tilt"] - 0.3) < 0.1)
    assert abs(pos["pan"]) < 0.1


def test_the_face_markers_follow_the_picture_of_a_digital_ptz(world):
    """The markers are drawn where the faces are in what the Soft PTZ shows,
    and not where they are in the whole picture, so that they stay on the
    face when it is moved or zoomed."""
    import time

    # Large enough to be found at the detector's default scale, and zoomed
    # only so much that it is all still in the picture
    source = world.make_face_source((0.5, 0.5), face_height=0.5)
    soft = world.unique("Soft PTZ")
    world.ws.call("CreateSourceFilter", {"sourceName": source, "filterName": soft, "filterKind": SOFT_KIND,
                                         "filterSettings": {"viewport": {"pan": 0.2, "tilt": 0.0, "zoom": 0.3}}})
    # The least gain, so that it does not move the viewport that is being looked at
    tracker = world.make_tracker(source, debug_faces=True, debug_always_show=True, Kp_x_db=-40.0, Kp_y_db=-40.0,
                                 Kp_z_db=-40.0, Ki_x=0.0, Ki_y=0.0, Ki_z=0.0, Td_x=0.0, Td_y=0.0, Td_z=0.0,
                                 track_z=1.0)

    box = {}

    def seen():
        image = world.screenshot(source)
        box["marker"] = bounding_box(image, is_marker)
        box["photo"] = bounding_box(image, is_photo, thickness=5)
        return box["marker"] is not None and box["photo"] is not None

    try:
        world.wait_for(seen, timeout=30, interval=1.0)
        time.sleep(1.0)
        seen()
    finally:
        tracker.remove()

    marker, photo = box["marker"], box["photo"]
    # The viewport is to the right of the middle, so the photo is to the left
    # of it, and the markers are on it, as they are where the face is
    assert photo[0] + photo[2] < 2 * 480 - 120, photo
    marker_x = (marker[0] + marker[2]) / 2
    photo_x = (photo[0] + photo[2]) / 2
    assert abs(marker_x - photo_x) < 25, (marker, photo)
    # and are as big as the face is on the photo, which is not as it is in the
    # whole picture, where they are 0.50 of it: it is 0.76 of the photo here
    ratio = (marker[3] - marker[1]) / (photo[3] - photo[1])
    assert 0.65 < ratio < 0.9, (ratio, marker, photo)


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
