/* SPDX-FileCopyrightText: 2026 Blender Authors
 *
 * SPDX-License-Identifier: GPL-2.0-or-later */

#pragma once

#include <cstddef>
#include <functional>
#include <string>

namespace blender::agent_bridge {

/** The transport owns bytes and jobs only. Dispatch is called exclusively by the main thread. */
using Dispatch = std::function<std::string(const std::string &request)>;

bool start(std::string &r_error);
void stop();
bool is_running();
std::string info_json();
void update_metadata(const std::string &json);
void poll(const Dispatch &dispatch);
bool cancellation_requested(const std::string &job_id);
void progress(const std::string &job_id, const std::string &phase, double fraction);
/** Complete a synchronous/deferred stage. Only the main-thread adapter calls this. */
bool finish(const std::string &job_id, const std::string &state, const std::string &response);
/** In-process requests use the same registry/queue, without a loopback socket. */
std::string request_json(const std::string &request);
/** Private bounded journal; accepted parameters and final outcomes stay off public events. */
bool set_journal(const std::string &path, size_t max_bytes, std::string &r_error);
/** Drop a terminal job's result references while retaining its idempotency tombstone. */
bool release_result(const std::string &job_id);

}  // namespace blender::agent_bridge
