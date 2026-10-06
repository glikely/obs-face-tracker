#include <obs-module.h>
#include <util/platform.h>
#include <cmath>
#include <algorithm>
#include "plugin-macros.generated.h"
#include "ptz-controller.hpp"
#include "helper.hpp"

// The API version this controller was written for: 0.7 is the first in which
// ptz_get_state may be called from any thread, which the tracker's is not
#define PTZ_API_MAJOR 0
#define PTZ_API_MINOR 7

#define LOOKUP_INTERVAL_NS (1000ULL * 1000 * 1000)
#define MOVE_INTERVAL_NS (50ULL * 1000 * 1000)
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

struct list_ctx
{
	obs_property_t *list;
};

static void list_filter(obs_source_t *, obs_source_t *filter, void *data)
{
	auto *ctx = (struct list_ctx *)data;
	if (ptz_controller::is_device(filter))
		obs_property_list_add_string(ctx->list, obs_source_get_name(filter), obs_source_get_uuid(filter));
}

static bool list_source(void *data, obs_source_t *source)
{
	auto *ctx = (struct list_ctx *)data;
	if (ptz_controller::is_device(source))
		obs_property_list_add_string(ctx->list, obs_source_get_name(source), obs_source_get_uuid(source));
	obs_source_enum_filters(source, list_filter, data);
	return true;
}

void ptz_controller::list_devices(obs_property_t *list)
{
	struct list_ctx ctx = {list};
	obs_enum_sources(list_source, &ctx);
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

	if (obs_data_has_user_value(state, "zoom"))
		zoom_pos = (float)obs_data_get_double(state, "zoom");
	blog(LOG_INFO, "'%s' is at pan %.2f tilt %.2f zoom %.2f", obs_source_get_name(source),
	     obs_data_get_double(state, "pan"), obs_data_get_double(state, "tilt"), zoom_pos.load());

	obs_data_release(state);
}

void ptz_controller::state_changed_cb(void *data, calldata_t *cd)
{
	auto *self = (ptz_controller *)data;
	auto *changed = (obs_data_t *)calldata_ptr(cd, "changed");
	if (changed && obs_data_has_user_value(changed, "zoom"))
		self->zoom_pos = (float)obs_data_get_double(changed, "zoom");
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
		if (!obs_weak_source_expired(weak))
			return;
		disconnect();
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

float ptz_controller::get_zoom() const
{
	return std::exp(zoom_pos.load() * std::log(zoom_max));
}
