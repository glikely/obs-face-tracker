#pragma once

#include <string>
#include <vector>
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
	std::atomic<float> pan_pos{0.0f}, tilt_pos{0.0f};
	float zoom_max = 20.0f;
	// A device that is a viewport on the picture, not a camera that moves
	std::atomic<bool> digital{false};
	std::atomic<float> digital_max_zoom{1.0f};
	// How fast it goes at full speed, as its settings have it
	std::atomic<float> digital_pantilt_rate{0.5f}, digital_zoom_rate{0.5f};
	uint64_t next_settings_ns = 0;
	void read_digital_settings(obs_source_t *source);
	float sent[3] = {0, 0, 0};
	bool moving = false;
	uint64_t log_ns = 0;
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

	// The zoom factor to scale the gain of pan and tilt by: a camera moves
	// the picture faster the more it is zoomed in, and a digital PTZ moves it
	// the same however far it is.
	float gain_zoom() const;

	// For a digital device, the part of the picture it shows, which a
	// tracker that sees the whole of it has to account for, as fractions of
	// it: its center from the top left, and its width and height, which are
	// the same fraction. Returns false for a camera, whose picture is what
	// it shows.
	bool viewport(float &center_x, float &center_y, float &size) const;

	static bool is_device(obs_source_t *source);
	static void list_devices(obs_property_t *list);

	struct device_info
	{
		std::string name, uuid;
		// The source the device shows, and its own if it is one
		std::string parent_uuid, parent_name;
		// What its settings say of how it connects, if they do
		std::string type, host;
		long long port;
	};
	static std::vector<device_info> find_devices();

	// Whether OBS has loaded the scene collection, so that its devices have
	// been made. Needs frontend_init(), and says false before it.
	static bool loading_finished();
	static void frontend_init();
	static void frontend_exit();
};
