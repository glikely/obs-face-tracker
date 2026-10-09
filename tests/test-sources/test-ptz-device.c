/*
 * A PTZ device for the integration tests in tests/integration.
 * It implements the PTZ device API of obs-ptz (docs/ptz-device-api.md) and
 * writes what it was asked to do to a file, which a test reads back.
 *
 * Settings:
 *   log_file   where to append what the device is asked to do
 *   api_major, api_minor   the API version it reports
 *   zoom       a position in [0, 1], announced with ptz_state_changed, and
 *              given by ptz_get_state, which any thread may call
 *   list_file  when it changes, write to it a line for each PTZ device of the
 *              API that OBS has, a source or a filter: its name, UUID and
 *              version, separated by tabs. A test cannot get a filter's UUID
 *              from obs-websocket.
 */

#include <obs-module.h>
#include <util/threading.h>
#include <stdio.h>
#include <stdarg.h>

struct test_ptz_device
{
	obs_source_t *source;
	pthread_mutex_t mutex;
	char *log_file;
	long long api_major, api_minor;
	double zoom;
};

static void log_line(struct test_ptz_device *d, const char *fmt, ...) __attribute__((format(printf, 2, 3)));

static void log_line(struct test_ptz_device *d, const char *fmt, ...)
{
	pthread_mutex_lock(&d->mutex);
	if (d->log_file && *d->log_file) {
		FILE *f = fopen(d->log_file, "a");
		if (f) {
			va_list ap;
			va_start(ap, fmt);
			vfprintf(f, fmt, ap);
			va_end(ap);
			fputc('\n', f);
			fclose(f);
		}
	}
	pthread_mutex_unlock(&d->mutex);
}

static void cb_get_api_version(void *data, calldata_t *cd)
{
	struct test_ptz_device *d = data;
	log_line(d, "version_query");
	calldata_set_int(cd, "major", d->api_major);
	calldata_set_int(cd, "minor", d->api_minor);
}

static void cb_move(void *data, calldata_t *cd)
{
	struct test_ptz_device *d = data;
	double pan = 0, tilt = 0, zoom = 0;
	calldata_get_float(cd, "pan", &pan);
	calldata_get_float(cd, "tilt", &tilt);
	calldata_get_float(cd, "zoom", &zoom);
	log_line(d, "move %.3f %.3f %.3f", pan, tilt, zoom);
}

static void cb_stop(void *data, calldata_t *cd)
{
	UNUSED_PARAMETER(cd);
	log_line(data, "stop");
}

static void cb_preset_recall(void *data, calldata_t *cd)
{
	const char *id = NULL;
	if (calldata_get_string(cd, "id", &id) && id)
		log_line(data, "preset_recall %s", id);
}

static void cb_get_state(void *data, calldata_t *cd)
{
	struct test_ptz_device *d = data;
	obs_data_t *state = calldata_ptr(cd, "state");
	if (!state)
		return;
	pthread_mutex_lock(&d->mutex);
	obs_data_set_double(state, "zoom", d->zoom);
	pthread_mutex_unlock(&d->mutex);
	obs_data_set_bool(state, "connected", true);
}

static void emit_zoom(struct test_ptz_device *d)
{
	obs_data_t *changed = obs_data_create();
	obs_data_set_double(changed, "zoom", d->zoom);

	calldata_t cd = {0};
	calldata_set_ptr(&cd, "source", d->source);
	calldata_set_ptr(&cd, "changed", changed);
	signal_handler_signal(obs_source_get_signal_handler(d->source), "ptz_state_changed", &cd);
	calldata_free(&cd);
	obs_data_release(changed);
}

struct list_ctx
{
	FILE *f;
};

static void list_device(struct list_ctx *ctx, obs_source_t *source)
{
	proc_handler_t *ph = obs_source_get_proc_handler(source);
	calldata_t cd = {0};
	if (ph && proc_handler_call(ph, "ptz_get_api_version", &cd))
		fprintf(ctx->f, "%s\t%s\t%lld.%lld\n", obs_source_get_name(source), obs_source_get_uuid(source),
			calldata_int(&cd, "major"), calldata_int(&cd, "minor"));
	calldata_free(&cd);
}

static void list_filter(obs_source_t *parent, obs_source_t *filter, void *param)
{
	UNUSED_PARAMETER(parent);
	list_device(param, filter);
}

static bool list_source(void *param, obs_source_t *source)
{
	list_device(param, source);
	obs_source_enum_filters(source, list_filter, param);
	return true;
}

static void write_device_list(const char *path)
{
	struct list_ctx ctx = {fopen(path, "w")};
	if (!ctx.f)
		return;
	obs_enum_sources(list_source, &ctx);
	fclose(ctx.f);
}

static void test_ptz_device_update(void *data, obs_data_t *settings)
{
	struct test_ptz_device *d = data;

	pthread_mutex_lock(&d->mutex);
	bfree(d->log_file);
	d->log_file = bstrdup(obs_data_get_string(settings, "log_file"));
	d->api_major = obs_data_get_int(settings, "api_major");
	d->api_minor = obs_data_get_int(settings, "api_minor");
	pthread_mutex_unlock(&d->mutex);

	const char *list_file = obs_data_get_string(settings, "list_file");
	if (*list_file)
		write_device_list(list_file);

	double zoom = obs_data_get_double(settings, "zoom");
	pthread_mutex_lock(&d->mutex);
	bool zoomed = zoom != d->zoom;
	d->zoom = zoom;
	pthread_mutex_unlock(&d->mutex);
	if (zoomed)
		emit_zoom(d);
}

static void test_ptz_device_defaults(obs_data_t *settings)
{
	obs_data_set_default_int(settings, "api_major", 0);
	obs_data_set_default_int(settings, "api_minor", 7);
	obs_data_set_default_double(settings, "zoom", 0.0);
}

static const char *test_ptz_device_name(void *unused)
{
	UNUSED_PARAMETER(unused);
	return "Face Tracker Test PTZ Device";
}

static void *test_ptz_device_create(obs_data_t *settings, obs_source_t *source)
{
	struct test_ptz_device *d = bzalloc(sizeof(struct test_ptz_device));
	d->source = source;
	pthread_mutex_init(&d->mutex, NULL);

	proc_handler_t *ph = obs_source_get_proc_handler(source);
	proc_handler_add(ph, "void ptz_get_api_version(out int major, out int minor)", cb_get_api_version, d);
	proc_handler_add(ph, "void ptz_move()", cb_move, d);
	proc_handler_add(ph, "void ptz_stop()", cb_stop, d);
	proc_handler_add(ph, "void ptz_preset_recall()", cb_preset_recall, d);
	proc_handler_add(ph, "ptr ptz_get_state(ptr state)", cb_get_state, d);

	signal_handler_t *sh = obs_source_get_signal_handler(source);
	signal_handler_add(sh, "void ptz_state_changed(ptr source, ptr changed)");

	test_ptz_device_update(d, settings);
	return d;
}

static void test_ptz_device_destroy(void *data)
{
	struct test_ptz_device *d = data;
	pthread_mutex_destroy(&d->mutex);
	bfree(d->log_file);
	bfree(d);
}

static void test_ptz_device_render(void *data, gs_effect_t *effect)
{
	UNUSED_PARAMETER(data);
	UNUSED_PARAMETER(effect);
}

static uint32_t test_ptz_device_size(void *data)
{
	UNUSED_PARAMETER(data);
	return 0;
}

void register_test_ptz_device(void)
{
	struct obs_source_info info = {
		.id = "face_tracker_test_ptz_device",
		.type = OBS_SOURCE_TYPE_INPUT,
		// Async, as the tracker is a filter of that kind only, and it may be
		// of the device as it is of a camera's source
		.output_flags = OBS_SOURCE_VIDEO | OBS_SOURCE_ASYNC,
		.get_name = test_ptz_device_name,
		.create = test_ptz_device_create,
		.destroy = test_ptz_device_destroy,
		.update = test_ptz_device_update,
		.get_defaults = test_ptz_device_defaults,
		.get_width = test_ptz_device_size,
		.get_height = test_ptz_device_size,
		.video_render = test_ptz_device_render,
	};
	obs_register_source(&info);
}
