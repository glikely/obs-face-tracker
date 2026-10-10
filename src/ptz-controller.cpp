#include <obs-module.h>
#include <obs-frontend-api.h>
#include <util/platform.h>
#include <cmath>
#include <cstring>
#include <algorithm>
#include "plugin-macros.generated.h"
#include "ptz-controller.hpp"
#include "helper.hpp"

// The obs-ptz device that shows a part of its source's picture
#define SOFT_PTZ_KIND "ca.secretlab.obs-ptz.soft-ptz"

// The API version this controller was written for: 0.7 is the first in which
// ptz_get_state may be called from any thread, which the tracker's is not
#define PTZ_API_MAJOR 0
#define PTZ_API_MINOR 7

#define LOOKUP_INTERVAL_NS (1000ULL * 1000 * 1000)
// Its own rates are in units a second, of which a pan unit is half the picture
#define DIGITAL_FULL_SPEED 1.0f
#define MOVE_INTERVAL_NS (50ULL * 1000 * 1000)
#define LOG_INTERVAL_NS (2ULL * 1000 * 1000 * 1000)
#define MOVE_EPSILON 0.005f

bool ptz_controller::is_device(obs_source_t *source)
{
	proc_handler_t *ph = obs_source_get_proc_handler(source);
	if (!ph)
		return false;

	calldata_t cd = {};
	bool ok = proc_handler_call(ph, "ptz_get_api_version", &cd) && calldata_int(&cd, "major") == PTZ_API_MAJOR &&
		  calldata_int(&cd, "minor") >= PTZ_API_MINOR;
	calldata_free(&cd);
	return ok;
}

void ptz_controller::list_devices(obs_property_t *list)
{
	// Devices of a kind have the same name, as a filter has on each source,
	// so say which source each is on
	for (const auto &d : find_devices()) {
		std::string label = d.name;
		if (!d.parent_name.empty() && d.parent_name != d.name)
			label += " (" + d.parent_name + ")";
		obs_property_list_add_string(list, label.c_str(), d.uuid.c_str());
	}
}

static void add_device_info(std::vector<ptz_controller::device_info> &out, obs_source_t *source)
{
	if (!ptz_controller::is_device(source))
		return;

	ptz_controller::device_info d;
	d.name = obs_source_get_name(source);
	d.uuid = obs_source_get_uuid(source);

	obs_source_t *parent = obs_source_get_type(source) == OBS_SOURCE_TYPE_FILTER ? obs_filter_get_parent(source)
										     : source;
	if (parent) {
		d.parent_uuid = obs_source_get_uuid(parent);
		d.parent_name = obs_source_get_name(parent);
	}

	obs_data_t *settings = obs_source_get_settings(source);
	d.type = obs_data_get_string(settings, "type");
	d.host = obs_data_get_string(settings, "host");
	d.port = obs_data_get_int(settings, "tcp_port");
	obs_data_release(settings);

	out.push_back(d);
}

static void find_filter(obs_source_t *, obs_source_t *filter, void *data)
{
	add_device_info(*(std::vector<ptz_controller::device_info> *)data, filter);
}

static bool find_source(void *data, obs_source_t *source)
{
	add_device_info(*(std::vector<ptz_controller::device_info> *)data, source);
	obs_source_enum_filters(source, find_filter, data);
	return true;
}

std::vector<ptz_controller::device_info> ptz_controller::find_devices()
{
	std::vector<device_info> devices;
	obs_enum_sources(find_source, &devices);
	return devices;
}

extern "C" void ft_ptz_frontend_init(void)
{
	ptz_controller::frontend_init();
}

extern "C" void ft_ptz_frontend_exit(void)
{
	ptz_controller::frontend_exit();
}

static std::atomic<bool> frontend_loaded{false};

static void frontend_event(enum obs_frontend_event event, void *)
{
	switch (event) {
	case OBS_FRONTEND_EVENT_FINISHED_LOADING:
	case OBS_FRONTEND_EVENT_SCENE_COLLECTION_CHANGED:
		frontend_loaded = true;
		break;
	case OBS_FRONTEND_EVENT_SCENE_COLLECTION_CHANGING:
		frontend_loaded = false;
		break;
	default:
		break;
	}
}

bool ptz_controller::loading_finished()
{
	return frontend_loaded;
}

void ptz_controller::frontend_init()
{
	obs_frontend_add_event_callback(frontend_event, nullptr);
}

void ptz_controller::frontend_exit()
{
	obs_frontend_remove_event_callback(frontend_event, nullptr);
}

ptz_controller::~ptz_controller()
{
	disconnect();
}

/*
 * A device says what changed, and not what it is, so one that is not
 * changing says nothing: a camera that was zoomed in when the tracker found it
 * would be taken for one that is not zoomed at all, until it zoomed.
 * Ask it once, then.
 */
void ptz_controller::read_state(obs_source_t *source)
{
	obs_data_t *state = obs_data_create();
	calldata_t cd = {};
	calldata_set_ptr(&cd, "state", state);
	proc_handler_call(obs_source_get_proc_handler(source), "ptz_get_state", &cd);
	calldata_free(&cd);

	if (obs_data_has_user_value(state, "pan"))
		pan_pos = (float)obs_data_get_double(state, "pan");
	if (obs_data_has_user_value(state, "tilt"))
		tilt_pos = (float)obs_data_get_double(state, "tilt");
	if (obs_data_has_user_value(state, "zoom"))
		zoom_pos = (float)obs_data_get_double(state, "zoom");
	blog(LOG_INFO, "'%s' is at pan %.2f tilt %.2f zoom %.2f", obs_source_get_name(source), pan_pos.load(),
	     tilt_pos.load(), zoom_pos.load());

	obs_data_release(state);
}

void ptz_controller::state_changed_cb(void *data, calldata_t *cd)
{
	auto *self = (ptz_controller *)data;
	auto *changed = (obs_data_t *)calldata_ptr(cd, "changed");
	if (!changed)
		return;
	if (obs_data_has_user_value(changed, "zoom"))
		self->zoom_pos = (float)obs_data_get_double(changed, "zoom");
	if (obs_data_has_user_value(changed, "pan"))
		self->pan_pos = (float)obs_data_get_double(changed, "pan");
	if (obs_data_has_user_value(changed, "tilt"))
		self->tilt_pos = (float)obs_data_get_double(changed, "tilt");
}

void ptz_controller::read_digital_settings(obs_source_t *source)
{
	// How much of the picture it magnifies at its full zoom, which is a
	// setting of the device and not part of its state
	obs_data_t *settings = obs_source_get_settings(source);
	double max_zoom = obs_data_get_double(settings, "max_zoom");
	digital_max_zoom = max_zoom >= 1.0 ? (float)max_zoom : 1.0f;

	double pantilt_rate = obs_data_get_double(settings, "pantilt_rate");
	double zoom_rate = obs_data_get_double(settings, "zoom_rate");
	digital_pantilt_rate = pantilt_rate > 0.0 ? (float)pantilt_rate : 0.5f;
	digital_zoom_rate = zoom_rate > 0.0 ? (float)zoom_rate : 0.5f;
	obs_data_release(settings);
}

bool ptz_controller::connect(obs_source_t *source)
{
	if (!is_device(source)) {
		if (!warned)
			blog(LOG_WARNING, "source '%s' does not implement a compatible PTZ device API",
			     obs_source_get_name(source));
		warned = true;
		return false;
	}

	signal_handler_t *sh = obs_source_get_signal_handler(source);
	signal_handler_connect(sh, "ptz_state_changed", state_changed_cb, this);
	weak = obs_source_get_weak_source(source);

	read_state(source);

	digital = strcmp(obs_source_get_id(source), SOFT_PTZ_KIND) == 0;
	if (digital) {
		blog(LOG_INFO, "'%s' is a digital PTZ, so the picture it shows is what is tracked",
		     obs_source_get_name(source));
		read_digital_settings(source);
	}
	return true;
}

void ptz_controller::disconnect()
{
	if (!weak)
		return;

	obs_source_t *source = obs_weak_source_get_source(weak);
	if (source) {
		signal_handler_t *sh = obs_source_get_signal_handler(source);
		signal_handler_disconnect(sh, "ptz_state_changed", state_changed_cb, this);
		obs_source_release(source);
	}
	obs_weak_source_release(weak);
	weak = nullptr;
	zoom_pos = 0.0f;
	pan_pos = 0.0f;
	tilt_pos = 0.0f;
	digital = false;
}

void ptz_controller::set_device(const char *uuid_)
{
	if (uuid_ && uuid == uuid_)
		return;

	// Do not leave the previous camera moving
	stop();
	disconnect();
	uuid = uuid_ ? uuid_ : "";
	warned = false;
	next_lookup_ns = 0;
	tick();
}

void ptz_controller::tick()
{
	if (uuid.empty())
		return;

	// The device may be gone, or not loaded yet; ask again later.
	if (weak) {
		if (obs_weak_source_expired(weak)) {
			disconnect();
		} else {
			// What a digital device magnifies can be changed
			uint64_t now = os_gettime_ns();
			if (digital && now >= next_settings_ns) {
				next_settings_ns = now + LOOKUP_INTERVAL_NS;
				if (obs_source_t *source = obs_weak_source_get_source(weak)) {
					read_digital_settings(source);
					obs_source_release(source);
				}
			}
			return;
		}
	}

	uint64_t ns = os_gettime_ns();
	if (ns < next_lookup_ns)
		return;
	next_lookup_ns = ns + LOOKUP_INTERVAL_NS;

	obs_source_t *source = obs_get_source_by_uuid(uuid.c_str());
	if (!source)
		return;
	connect(source);
	obs_source_release(source);
}

obs_source_t *ptz_controller::get_source()
{
	return weak ? obs_weak_source_get_source(weak) : nullptr;
}

void ptz_controller::call(const char *proc, calldata_t *cd)
{
	obs_source_t *source = get_source();
	if (!source)
		return;
	proc_handler_call(obs_source_get_proc_handler(source), proc, cd);
	obs_source_release(source);
}

void ptz_controller::move(float pan, float tilt, float zoom)
{
	// The speeds are for a camera. A digital PTZ's full speed is its rate
	// setting, which is slow at its default, and the picture then follows a
	// face over many seconds. So take them as of a device whose full speed is
	// DIGITAL_FULL_SPEED, whatever the rate is. It is where tracking a face
	// came out smooth and without much overshoot, with Soft PTZ at its
	// default rates, and a quarter of it was too slow to be seen to move.
	if (digital) {
		const float pt = DIGITAL_FULL_SPEED / digital_pantilt_rate.load();
		const float z = DIGITAL_FULL_SPEED / digital_zoom_rate.load();
		pan *= pt;
		tilt *= pt;
		zoom *= z;
	}
	const float v[3] = {std::clamp(pan, -1.0f, 1.0f), std::clamp(tilt, -1.0f, 1.0f),
			    std::clamp(zoom, -1.0f, 1.0f)};

	// The device keeps moving at the last speed, so only changes are sent.
	if (std::fabs(v[0] - sent[0]) < MOVE_EPSILON && std::fabs(v[1] - sent[1]) < MOVE_EPSILON &&
	    std::fabs(v[2] - sent[2]) < MOVE_EPSILON)
		return;

	// Do not flood the camera, except to stop it.
	uint64_t ns = os_gettime_ns();
	bool stopping = v[0] == 0.0f && v[1] == 0.0f && v[2] == 0.0f;
	if (!stopping && ns < sent_ns + MOVE_INTERVAL_NS)
		return;

	CALLDATA_FIXED_DECL(cd, 128);
	calldata_set_float(&cd, "pan", v[0]);
	calldata_set_float(&cd, "tilt", v[1]);
	calldata_set_float(&cd, "zoom", v[2]);
	call("ptz_move", &cd);

	// What it is asked, for the log: not each change, as a dead zone makes
	// that go back and forth, but what it was asked, every few seconds
	moving = !stopping;
	if (ns >= log_ns) {
		log_ns = ns + LOG_INTERVAL_NS;
		if (moving) {
			float cx, cy, size;
			if (viewport(cx, cy, size))
				blog(LOG_INFO,
				     "asked the PTZ device to move: pan %.2f tilt %.2f zoom %.2f; it shows %.0f%% of the picture around %.2f, %.2f",
				     v[0], v[1], v[2], size * 100.0f, cx, cy);
			else
				blog(LOG_INFO, "asked the PTZ device to move: pan %.2f tilt %.2f zoom %.2f", v[0], v[1],
				     v[2]);
		}
	}

	std::copy(v, v + 3, sent);
	sent_ns = ns;
}

void ptz_controller::stop()
{
	std::fill(sent, sent + 3, 0.0f);
	CALLDATA_FIXED_DECL(cd, 128);
	call("ptz_stop", &cd);
}

void ptz_controller::recall_preset(int slot)
{
	// The API names a preset by a string, which for a camera's own slot is
	// "camera:<n>"
	char id[32];
	snprintf(id, sizeof(id), "camera:%d", slot);

	CALLDATA_FIXED_DECL(cd, 128);
	calldata_set_string(&cd, "id", id);
	call("ptz_preset_recall", &cd);
}

float ptz_controller::gain_zoom() const
{
	return digital ? 1.0f : get_zoom();
}

float ptz_controller::get_zoom() const
{
	// A digital device knows how much it magnifies
	const float max = digital ? digital_max_zoom.load() : zoom_max;
	return std::exp(zoom_pos.load() * std::log(max));
}

bool ptz_controller::viewport(float &center_x, float &center_y, float &size) const
{
	if (!digital)
		return false;
	// Pan and tilt are in [-1, 1] and a unit is half the picture, with tilt
	// up while the picture's y runs down; it shows 1/scale of each side
	center_x = 0.5f + pan_pos.load() * 0.5f;
	center_y = 0.5f - tilt_pos.load() * 0.5f;
	size = 1.0f / get_zoom();
	return true;
}
