#pragma once

#include <string>
#include <atomic>
#include <obs.h>

/*
 * Controller side of the PTZ device API (obs-ptz docs/ptz-device-api.md).
 * The device is found by the UUID of its source.
 * move() is meant to be called from one thread, the others from any.
 */
class ptz_controller {
	std::string uuid;
	obs_weak_source_t *weak = nullptr;
	uint64_t next_lookup_ns = 0;
	bool warned = false;
	std::atomic<float> zoom_pos{0.0f};
	float zoom_max = 20.0f;
	float sent[3] = {0, 0, 0};
	uint64_t sent_ns = 0;

	static void state_changed_cb(void *data, calldata_t *cd);
	bool connect(obs_source_t *source);
	void disconnect();
	obs_source_t *get_source();
	void call(const char *proc, calldata_t *cd);

	// Reads pan, tilt and zoom from the state of a device, which any thread
	// may ask for
	void read_state(obs_source_t *source);

public:
	ptz_controller() = default;
	~ptz_controller();

	void set_device(const char *uuid);
	void set_zoom_max(float zoom_max_) { zoom_max = zoom_max_ > 1.0f ? zoom_max_ : 1.0f; }

	// Look for the device again if it is not connected yet.
	void tick();

	// Speeds are in [-1, 1]. Positive is right, up and tele.
	// Repeats and updates that come too fast are not sent.
	void move(float pan, float tilt, float zoom);
	void stop();
	// A preset slot of the camera
	void recall_preset(int slot);

	// Zoom factor as the device last reported, 1.0 if it has not.
	float get_zoom() const;

	static bool is_device(obs_source_t *source);
	static void list_devices(obs_property_t *list);
};
