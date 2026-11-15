/* SPDX-FileCopyrightText: 2026 Blender Authors
 *
 * SPDX-License-Identifier: GPL-2.0-or-later */

/** \file
 * Local framed transport and bounded job registry. No Blender/Python/GPU access on the I/O thread.
 */

#include "AGENT_service.hh"
#include "BLI_serialize.hh"

#include <algorithm>
#include <array>
#include <atomic>
#include <cerrno>
#include <chrono>
#include <cmath>
#include <cstdlib>
#include <cstring>
#include <deque>
#include <fstream>
#include <iomanip>
#include <map>
#include <mutex>
#include <sstream>
#include <thread>
#include <vector>

#include <fcntl.h>
#include <poll.h>
#include <sys/eventfd.h>
#include <sys/random.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/un.h>
#include <unistd.h>

namespace blender::agent_bridge {
namespace {
using namespace io::serialize;
using Json = std::shared_ptr<Value>;
using Dict = std::shared_ptr<DictionaryValue>;
using Clock = std::chrono::steady_clock;
constexpr size_t max_frame = 8 * 1024 * 1024;
constexpr size_t max_jobs = 128;
constexpr size_t max_pending = 64;
constexpr size_t max_clients = 8;
constexpr size_t max_events = 1024;

Json parse(const std::string &text)
{
  /* Bound conversion recursion too, including for malformed/untrusted input. */
  int depth = 0;
  bool quoted = false, escaped = false;
  for (const char ch : text) {
    if (quoted) {
      if (escaped) {
        escaped = false;
      }
      else if (ch == '\\') {
        escaped = true;
      }
      else if (ch == '"') {
        quoted = false;
      }
    }
    else if (ch == '"') {
      quoted = true;
    }
    else if (ch == '[' || ch == '{') {
      if (++depth > 64) {
        return nullptr;
      }
    }
    else if (ch == ']' || ch == '}') {
      depth--;
    }
  }
  std::istringstream input(text);
  Json result(JsonFormatter().deserialize(input).release());
  input >> std::ws;
  return input.eof() ? result : nullptr;
}

std::string encode(const Value &value)
{
  std::ostringstream output;
  JsonFormatter().serialize(output, value);
  return output.str();
}

std::string string_field(const DictionaryValue &dict, const char *key)
{
  const auto value = dict.lookup_str(key);
  return value ? std::string(*value) : std::string();
}

std::string bounded_utf8(const std::string &value, const size_t max_bytes)
{
  size_t end = std::min(value.size(), max_bytes);
  while (end > 0 && end < value.size() && (uint8_t(value[end]) & 0xc0) == 0x80) {
    end--;
  }
  return value.substr(0, end);
}

Json field(const DictionaryValue &dict, const char *key)
{
  const auto *value = dict.lookup(key);
  return value ? *value : std::make_shared<NullValue>();
}

Json canonical_value(const Json &value)
{
  if (const DictionaryValue *dict = value->as_dictionary_value()) {
    auto result = std::make_shared<DictionaryValue>();
    std::vector<std::string> names;
    for (const auto &[name, item] : dict->elements()) {
      names.push_back(name);
    }
    std::sort(names.begin(), names.end());
    for (const std::string &name : names) {
      result->append(name, canonical_value(field(*dict, name.c_str())));
    }
    return result;
  }
  if (const ArrayValue *array = value->as_array_value()) {
    auto result = std::make_shared<ArrayValue>();
    for (const auto &item : array->elements()) {
      result->append(canonical_value(item));
    }
    return result;
  }
  return value;
}

Dict response(const Json &id, Json result)
{
  auto out = std::make_shared<DictionaryValue>();
  out->append_str("jsonrpc", "2.0");
  out->append("id", id);
  out->append("result", std::move(result));
  return out;
}

Dict error(const Json &id, const int code, const char *kind, const std::string &message)
{
  auto out = std::make_shared<DictionaryValue>();
  out->append_str("jsonrpc", "2.0");
  out->append("id", id);
  auto detail = out->append_dict("error");
  detail->append_int("code", code);
  detail->append_str("message", message);
  detail->append_dict("data")->append_str("kind", kind);
  return out;
}

std::string random_hex()
{
  std::array<unsigned char, 32> bytes;
  size_t offset = 0;
  while (offset < bytes.size()) {
    const ssize_t count = getrandom(bytes.data() + offset, bytes.size() - offset, 0);
    if (count < 0 && errno == EINTR) {
      continue;
    }
    if (count <= 0) {
      return {};
    }
    offset += size_t(count);
  }
  std::ostringstream out;
  for (const unsigned char byte : bytes) {
    out << std::hex << std::setw(2) << std::setfill('0') << unsigned(byte);
  }
  return out.str();
}

struct Client {
  int fd = -1;
  uint64_t serial = 0;
  std::string identity;
  bool authenticated = false;
  std::string input;
  std::string output;
  size_t sent = 0;
  int outstanding = 0;
};

struct Pending {
  uint64_t client = 0;
  Json request;
  std::string job_id;
  bool control = false;
};

struct Waiter {
  uint64_t client;
  Json id;
  std::string job_id;
  Clock::time_point deadline;
};

struct Job {
  std::string id;
  std::string key;
  std::string payload;
  std::string state = "queued";
  std::string phase = "queued";
  double fraction = 0.0;
  bool progress_known = false;
  bool cancel = false;
  Json outcome;
  double elapsed_ms = 0.0;
  Clock::time_point stage_started;
  bool stage_active = false;
  uint64_t order = 0;
  std::string label;
  bool candidate_mode = false;
  uint64_t event_cursor = 0;
  Clock::time_point last_progress_event;
  bool resources_released = false;
  bool auto_apply = false;
};

class Service {
 public:
  std::mutex mutex;
  std::atomic<bool> running{false};
  std::thread thread;
  int listener = -1;
  int wake_fd = -1;
  std::string directory;
  std::string endpoint;
  std::string token;
  std::string session;
  Json metadata;
  std::map<uint64_t, Client> clients;
  std::deque<Pending> pending;
  std::vector<Waiter> waiters;
  std::map<std::string, Job> jobs;
  std::map<std::string, std::string> keys;
  uint64_t next_client = 1;
  uint64_t next_job = 1;
  bool stop_when_flushed = false;
  Clock::time_point stop_deadline;
  std::deque<Dict> events;
  uint64_t event_cursor = 0;
  int journal_fd = -1;
  std::string journal_path;
  bool journal_failed = false;
  bool journal_full = false;
  std::deque<std::string> journal_pending;
  size_t journal_bytes = 0;
  size_t journal_pending_bytes = 0;
  size_t journal_limit = 16 * 1024 * 1024;
  bool journal_needs_sync = false;
  Clock::time_point journal_last_sync;

  void flush_journal(const bool sync = false)
  {
    while (journal_fd >= 0 && !journal_pending.empty() && !journal_failed) {
      const std::string &record = journal_pending.front();
      size_t offset = 0;
      while (offset < record.size()) {
        const ssize_t count = write(journal_fd, record.data() + offset, record.size() - offset);
        if (count < 0 && errno == EINTR) {
          continue;
        }
        if (count <= 0) {
          journal_failed = true;
          break;
        }
        offset += size_t(count);
      }
      journal_bytes += offset;
      journal_pending_bytes -= record.size();
      journal_pending.pop_front();
      journal_needs_sync = true;
    }
    if (journal_fd >= 0 && journal_needs_sync &&
        (sync || Clock::now() - journal_last_sync > std::chrono::milliseconds(500)))
    {
      journal_failed |= fdatasync(journal_fd) != 0;
      journal_needs_sync = false;
      journal_last_sync = Clock::now();
    }
    if (journal_failed) {
      journal_pending.clear();
      journal_pending_bytes = 0;
    }
  }

  void emit(const char *type, Job *job = nullptr)
  {
    auto event = std::make_shared<DictionaryValue>();
    event->append_int("cursor", ++event_cursor);
    event->append_str("session_id", session);
    event->append_str("type", type);
    event->append_double("time_unix_ms",
                         std::chrono::duration<double, std::milli>(
                             std::chrono::system_clock::now().time_since_epoch())
                             .count());
    if (job) {
      job->event_cursor = event_cursor;
      event->append("job", job_status(*job, false));
    }
    events.push_back(event);
    if (events.size() > max_events) {
      events.pop_front();
    }
    if (journal_fd >= 0 && !journal_failed && !journal_full) {
      /* Private provenance is kept off the compact public event stream. This is not a
       * mutation WAL: buffered appends can be lost in a crash and are never auto-replayed. */
      auto record = std::make_shared<DictionaryValue>();
      for (const auto &[name, value] : event->elements()) {
        record->append(name, value);
      }
      if (job && std::strcmp(type, "job.accepted") == 0) {
        record->append("accepted_parameters", parse(job->payload));
      }
      if (job && std::strcmp(type, "job.finished") == 0 && job->outcome) {
        record->append("outcome", job->outcome);
      }
      const std::string line = encode(*record) + "\n";
      if (journal_pending.size() >= max_events ||
          journal_bytes + journal_pending_bytes + line.size() > journal_limit)
      {
        journal_full = true;
      }
      else {
        journal_pending.push_back(line);
        journal_pending_bytes += line.size();
      }
    }
    wake_io();
  }

  ~Service()
  {
    shutdown();
  }

  void shutdown()
  {
    running = false;
    wake_io();
    if (thread.joinable()) {
      thread.join();
    }
    std::lock_guard lock(mutex);
    if (!session.empty()) {
      for (auto &[id, job] : jobs) {
        if (job.state == "queued" || job.state == "running" || job.state == "applying" ||
            job.state == "discarding" || job.state == "ready")
        {
          job.state = "interrupted";
          job.phase = "service_stopped";
          emit("job.interrupted", &job);
        }
      }
      emit("session.closed");
      flush_journal(true);
    }
    if (journal_fd >= 0) {
      close(journal_fd);
      journal_fd = -1;
    }
    for (auto &[serial, client] : clients) {
      close(client.fd);
    }
    clients.clear();
    if (listener >= 0) {
      close(listener);
      listener = -1;
    }
    if (wake_fd >= 0) {
      close(wake_fd);
      wake_fd = -1;
    }
    if (!directory.empty()) {
      unlink(endpoint.c_str());
      unlink((directory + "/session.json").c_str());
      rmdir(directory.c_str());
    }
    pending.clear();
    waiters.clear();
    jobs.clear();
    keys.clear();
    events.clear();
    event_cursor = 0;
    journal_pending.clear();
    journal_path.clear();
    journal_failed = false;
    journal_full = false;
    journal_bytes = 0;
    journal_pending_bytes = 0;
    journal_limit = 16 * 1024 * 1024;
    journal_needs_sync = false;
    directory.clear();
    endpoint.clear();
    token.clear();
    session.clear();
    metadata.reset();
    stop_when_flushed = false;
  }

  void wake_io()
  {
    if (wake_fd >= 0) {
      const uint64_t one = 1;
      /* EAGAIN means a wakeup is already pending. This never touches Blender state. */
      const ssize_t ignored = write(wake_fd, &one, sizeof(one));
      (void)ignored;
    }
  }

  void send(Client &client, const Value &value)
  {
    if (client.sent) {
      client.output.erase(0, client.sent);
      client.sent = 0;
    }
    std::string payload = encode(value);
    if (payload.size() > max_frame) {
      const DictionaryValue *envelope = value.as_dictionary_value();
      payload = encode(*error(envelope ? field(*envelope, "id") : std::make_shared<NullValue>(),
                              -32001,
                              "RESULT_TOO_LARGE",
                              "Result exceeds frame limit; use a bounded query or an artifact"));
    }
    if (client.output.size() - client.sent + payload.size() > max_frame * 2) {
      /* A slow client is disconnected rather than accumulating unbounded memory. */
      ::shutdown(client.fd, SHUT_RDWR);
      return;
    }
    const uint32_t length = uint32_t(payload.size());
    const char header[4] = {
        char(length >> 24), char(length >> 16), char(length >> 8), char(length)};
    client.output.append(header, 4);
    client.output += payload;
    wake_io();
  }

  Dict job_status(const Job &job, const bool include_result = true)
  {
    auto value = std::make_shared<DictionaryValue>();
    value->append_str("job_id", job.id);
    value->append_str("state", job.state);
    value->append_str("phase", job.phase);
    value->append_str("label", job.label);
    value->append_bool("cancel_requested", job.cancel);
    value->append_int("event_cursor", job.event_cursor);
    value->append_bool("resources_released", job.resources_released);
    value->append_bool("auto_apply", job.auto_apply);
    if (job.progress_known) {
      value->append_double("progress", job.fraction);
    }
    else {
      value->append("progress", std::make_shared<NullValue>());
    }
    value->append_double("elapsed_ms",
                         job.elapsed_ms + (job.stage_active ?
                                               std::chrono::duration<double, std::milli>(
                                                   Clock::now() - job.stage_started)
                                                   .count() :
                                               0.0));
    if (job.outcome && include_result) {
      value->append("outcome", job.outcome);
    }
    return value;
  }

  bool job_active(const Job &job)
  {
    return job.state == "queued" || job.state == "running" || job.state == "applying" ||
           job.state == "discarding" || (job.state == "ready" && job.auto_apply);
  }

  void resolve_waiters()
  {
    const auto now = Clock::now();
    std::erase_if(waiters, [&](const Waiter &waiter) {
      const auto client = clients.find(waiter.client);
      if (client == clients.end()) {
        return true;
      }
      const Job &job = jobs.at(waiter.job_id);
      if (!job_active(job) || now >= waiter.deadline) {
        auto result = job_status(job);
        result->append_bool("wait_timed_out", job_active(job));
        send(client->second, *response(waiter.id, result));
        client->second.outstanding--;
        return true;
      }
      return false;
    });
  }

  void receive(Client &client, const std::string &payload)
  {
    const Json request = parse(payload);
    const DictionaryValue *dict = request ? request->as_dictionary_value() : nullptr;
    if (!dict) {
      send(client,
           *error(std::make_shared<NullValue>(), -32700, "PARSE_ERROR", "Expected JSON object"));
      return;
    }
    Json id = field(*dict, "id");
    if ((id->type() != eValueType::Null && id->type() != eValueType::Int &&
         id->type() != eValueType::Double && id->type() != eValueType::String) ||
        (id->as_string_value() && id->as_string_value()->value().size() > 256))
    {
      send(client,
           *error(std::make_shared<NullValue>(),
                  -32600,
                  "INVALID_REQUEST",
                  "Request id must be a number, null or a string up to 256 bytes"));
      return;
    }
    const std::string method = string_field(*dict, "method");
    const DictionaryValue *params = dict->lookup_dict("params");
    if (string_field(*dict, "jsonrpc") != "2.0" || method.empty() ||
        (!id->as_string_value() && !id->as_int_value()))
    {
      send(client, *error(id, -32600, "INVALID_REQUEST", "JSON-RPC 2.0 and request id required"));
      return;
    }
    if (!client.authenticated) {
      if (method != "bridge.hello" || !params || string_field(*params, "token") != token ||
          string_field(*params, "client_id").empty() ||
          string_field(*params, "client_id").size() > 64)
      {
        send(client, *error(id, -32000, "UNAUTHORIZED", "Authenticated hello required"));
        return;
      }
      if (params->lookup_int("protocol_major").value_or(0) != 1) {
        send(client, *error(id, -32000, "UNSUPPORTED_VERSION", "Expected protocol major 1"));
        return;
      }
      client.authenticated = true;
      client.identity = string_field(*params, "client_id");
    }
    if (method == "bridge.hello" && metadata) {
      auto cached = std::make_shared<DictionaryValue>();
      for (const auto &[name, value] : metadata->as_dictionary_value()->elements()) {
        cached->append(name, value);
      }
      cached->append_bool("scene_state_cached", true);
      send(client, *response(id, cached));
      return;
    }
    if (method == "job.status" || method == "job.cancel") {
      const std::string job_id = params ? string_field(*params, "job_id") : "";
      auto found = jobs.find(job_id);
      if (found == jobs.end()) {
        send(client, *error(id, -32602, "UNKNOWN_JOB", "Unknown job id"));
        return;
      }
      Job &job = found->second;
      if (method == "job.cancel" && (job.state == "queued" || job.state == "running" ||
                                     job.state == "ready" || job.state == "applying"))
      {
        job.cancel = true;
        if (job.state == "queued" || job.state == "ready") {
          job.state = "cancelled";
          job.phase = "cancelled";
        }
        emit("job.cancel_requested", &job);
      }
      const bool include_result = !params || params->lookup_bool("include_result").value_or(true);
      send(client, *response(id, job_status(job, include_result)));
      return;
    }
    if (method == "job.wait") {
      const std::string job_id = params ? string_field(*params, "job_id") : "";
      const auto job = jobs.find(job_id);
      if (job == jobs.end()) {
        send(client, *error(id, -32602, "UNKNOWN_JOB", "Unknown job id"));
        return;
      }
      if (client.outstanding >= 8 || waiters.size() >= max_pending || stop_when_flushed) {
        send(client, *error(id, -32001, "BUSY", "Waiter limit reached or service stopping"));
        return;
      }
      const int64_t duration = std::clamp<int64_t>(
          params->lookup_int("timeout_ms").value_or(25000), 1, 25000);
      if (!job_active(job->second)) {
        auto result = job_status(job->second);
        result->append_bool("wait_timed_out", false);
        send(client, *response(id, result));
      }
      else {
        waiters.push_back(
            {client.serial, id, job_id, Clock::now() + std::chrono::milliseconds(duration)});
        client.outstanding++;
      }
      return;
    }
    if (method == "job.list") {
      const int limit = int(std::clamp<int64_t>(
          params ? params->lookup_int("limit").value_or(20) : 20, 1, max_jobs));
      std::vector<const Job *> ordered;
      for (const auto &[id, job] : jobs) {
        ordered.push_back(&job);
      }
      std::sort(ordered.begin(), ordered.end(), [](const Job *a, const Job *b) {
        return a->order > b->order;
      });
      auto result = std::make_shared<ArrayValue>();
      for (int i = 0; i < std::min<int>(limit, ordered.size()); i++) {
        result->append(job_status(*ordered[i], false));
      }
      send(client, *response(id, result));
      return;
    }
    if (method == "events.poll") {
      const int64_t after = params ? params->lookup_int("after").value_or(0) : 0;
      const int limit = int(
          std::clamp<int64_t>(params ? params->lookup_int("limit").value_or(64) : 64, 1, 256));
      const int64_t oldest = events.empty() ? int64_t(event_cursor + 1) :
                                              events.front()->lookup_int("cursor").value();
      if (after < oldest - 1 || after > int64_t(event_cursor)) {
        send(client,
             *error(id, -32602, "RESET_REQUIRED", "Event cursor is outside retained history"));
        return;
      }
      auto result = std::make_shared<DictionaryValue>();
      auto selected = result->append_array("events");
      int64_t cursor = after;
      for (const auto &event : events) {
        if (event->lookup_int("cursor").value() > after &&
            selected->elements().size() < size_t(limit))
        {
          selected->append(event);
          cursor = event->lookup_int("cursor").value();
        }
      }
      result->append_int("next_cursor", cursor);
      result->append_int("latest_cursor", event_cursor);
      send(client, *response(id, result));
      return;
    }
    if (method == "bridge.stop") {
      auto result = std::make_shared<DictionaryValue>();
      result->append_bool("stopping", true);
      send(client, *response(id, result));
      if (!stop_when_flushed) {
        stop_deadline = Clock::now() + std::chrono::milliseconds(250);
      }
      stop_when_flushed = true;
      return;
    }
    if (pending.size() >= max_pending || client.outstanding >= 8 || stop_when_flushed) {
      send(client, *error(id, -32001, "BUSY", "Request queue full or service stopping"));
      return;
    }
    if (method == "job.apply" || method == "job.discard") {
      const std::string job_id = params ? string_field(*params, "job_id") : "";
      auto found = jobs.find(job_id);
      if (found == jobs.end()) {
        send(client, *error(id, -32602, "UNKNOWN_JOB", "Unknown job id"));
        return;
      }
      Job &job = found->second;
      if (!job.candidate_mode) {
        send(client, *error(id, -32602, "JOB_STATE", "Job has no staged candidate"));
        return;
      }
      if ((method == "job.apply" && (job.state == "applying" || job.state == "succeeded")) ||
          (method == "job.discard" && (job.state == "discarding" || job.state == "discarded")))
      {
        send(client, *response(id, job_status(job)));
        return;
      }
      if (job.state != "ready" || job.cancel) {
        send(client, *error(id, -32602, "JOB_STATE", "Job is not an applicable ready candidate"));
        return;
      }
      job.state = method == "job.apply" ? "applying" : "discarding";
      job.phase = job.state;
      job.progress_known = false;
      emit(method == "job.apply" ? "job.applying" : "job.discarding", &job);
      pending.push_back({0, request, job.id, true});
      send(client, *response(id, job_status(job)));
      return;
    }
    if (method == "job.submit") {
      if (!params || string_field(*params, "operation_key").empty() ||
          string_field(*params, "operation").empty() ||
          string_field(*params, "operation_key").size() > 256 ||
          string_field(*params, "operation").size() > 128)
      {
        send(client, *error(id, -32602, "INVALID_PARAMS", "Operation and operation_key required"));
        return;
      }
      const std::string key = std::to_string(client.identity.size()) + ":" + client.identity +
                              string_field(*params, "operation_key");
      const std::string canonical = encode(*canonical_value(field(*dict, "params")));
      if (canonical.size() > 512 * 1024) {
        send(client, *error(id, -32602, "INVALID_PARAMS", "Job parameters exceed 512 KiB"));
        return;
      }
      const auto previous = keys.find(key);
      if (previous != keys.end()) {
        const Job &job = jobs.at(previous->second);
        send(client,
             job.payload == canonical ? *response(id, job_status(job)) :
                                        *error(id,
                                               -32602,
                                               "IDEMPOTENCY_CONFLICT",
                                               "Operation key already used for different inputs"));
        return;
      }
      if (jobs.size() >= max_jobs) {
        send(client, *error(id, -32001, "BUSY", "Session job retention limit reached"));
        return;
      }
      Job job;
      job.id = "job-" + std::to_string(next_job++);
      job.order = next_job - 1;
      job.candidate_mode = string_field(*params, "execution_mode") == "worker_candidate";
      job.auto_apply = job.candidate_mode &&
                       string_field(*params, "apply_mode") == "auto_if_valid";
      const DictionaryValue *recipe = params->lookup_dict("recipe");
      job.label = recipe ? string_field(*recipe, "id") : string_field(*params, "operation");
      job.label = bounded_utf8(job.label, 160);
      job.key = key;
      job.payload = canonical;
      keys.emplace(key, job.id);
      jobs.emplace(job.id, job);
      emit("job.accepted", &jobs.at(job.id));
      auto execution = std::make_shared<DictionaryValue>();
      execution->append_str("jsonrpc", "2.0");
      execution->append("id", id);
      execution->append_str("method", string_field(*params, "operation"));
      auto execution_params = execution->append_dict("params");
      for (const auto &[name, value] : params->elements()) {
        if (name != "_job_id") {
          execution_params->append(name, value);
        }
      }
      execution_params->append_str("_job_id", job.id);
      pending.push_back({0, execution, job.id});
      send(client, *response(id, job_status(jobs.at(job.id))));
      return;
    }
    if (method != "bridge.hello" && method != "bridge.status" && method != "scene.inspect" &&
        method != "scene.changes" && method != "recipes.list" && method != "recipes.describe" &&
        method != "artifacts.list" && method != "artifacts.read" && method != "history.sessions" &&
        method != "history.job" && method != "capture.pick" && method != "storage.stats")
    {
      send(client,
           *error(id, -32601, "METHOD_NOT_FOUND", "Unknown method; submit operations as jobs"));
      return;
    }
    pending.push_back({client.serial, request, ""});
    client.outstanding++;
  }

  void io_loop()
  {
    while (running) {
      std::vector<pollfd> descriptors;
      std::vector<uint64_t> serials;
      descriptors.push_back({listener, POLLIN, 0});
      descriptors.push_back({wake_fd, POLLIN, 0});
      {
        std::lock_guard lock(mutex);
        for (const auto &[serial, client] : clients) {
          descriptors.push_back(
              {client.fd, short(POLLIN | (client.output.empty() ? 0 : POLLOUT)), 0});
          serials.push_back(serial);
        }
      }
      if (::poll(descriptors.data(), descriptors.size(), 20) < 0) {
        if (errno == EINTR) {
          continue;
        }
        break;
      }
      std::lock_guard lock(mutex);
      flush_journal();
      if (descriptors[1].revents & POLLIN) {
        uint64_t count;
        const ssize_t ignored = read(wake_fd, &count, sizeof(count));
        (void)ignored;
      }
      resolve_waiters();
      if (descriptors[0].revents & POLLIN) {
        const int fd = accept4(listener, nullptr, nullptr, SOCK_CLOEXEC | SOCK_NONBLOCK);
        if (fd >= 0) {
          struct ucred credentials;
          socklen_t size = sizeof(credentials);
          if (clients.size() >= max_clients ||
              getsockopt(fd, SOL_SOCKET, SO_PEERCRED, &credentials, &size) != 0 ||
              credentials.uid != getuid())
          {
            close(fd);
          }
          else {
            const uint64_t serial = next_client++;
            Client client;
            client.fd = fd;
            client.serial = serial;
            clients.emplace(serial, std::move(client));
          }
        }
      }
      for (size_t index = 0; index < serials.size(); index++) {
        auto found = clients.find(serials[index]);
        if (found == clients.end()) {
          continue;
        }
        Client &client = found->second;
        const short events = descriptors[index + 2].revents;
        bool dead = events & (POLLERR | POLLHUP | POLLNVAL);
        if (!dead && (events & POLLIN)) {
          std::array<char, 65536> buffer;
          const ssize_t size = recv(client.fd, buffer.data(), buffer.size(), 0);
          if (size > 0) {
            client.input.append(buffer.data(), size_t(size));
            while (client.input.size() >= 4) {
              const auto *bytes = reinterpret_cast<const unsigned char *>(client.input.data());
              const uint32_t length = uint32_t(bytes[0]) << 24 | uint32_t(bytes[1]) << 16 |
                                      uint32_t(bytes[2]) << 8 | uint32_t(bytes[3]);
              if (length == 0 || length > max_frame) {
                dead = true;
                break;
              }
              if (client.input.size() < size_t(length) + 4) {
                break;
              }
              receive(client, client.input.substr(4, length));
              client.input.erase(0, size_t(length) + 4);
            }
          }
          else if (size == 0 || (errno != EAGAIN && errno != EINTR)) {
            dead = true;
          }
        }
        if (!dead && (events & POLLOUT) && !client.output.empty()) {
          const ssize_t size = ::send(client.fd,
                                      client.output.data() + client.sent,
                                      client.output.size() - client.sent,
                                      MSG_NOSIGNAL);
          if (size > 0) {
            client.sent += size_t(size);
            if (client.sent == client.output.size()) {
              client.output.clear();
              client.sent = 0;
            }
          }
          else if (size < 0 && errno != EAGAIN && errno != EINTR) {
            dead = true;
          }
        }
        if (dead) {
          close(client.fd);
          clients.erase(found);
        }
      }
      if (stop_when_flushed &&
          (Clock::now() >= stop_deadline ||
           std::all_of(clients.begin(),
                       clients.end(),
                       [](const auto &item) { return item.second.output.empty(); })))
      {
        running = false;
      }
    }
    running = false;
  }
};

Service service;
}  // namespace

bool start(std::string &r_error)
{
  if (service.running) {
    r_error = "Bridge is already running";
    return false;
  }
  service.shutdown();
  service.token = random_hex();
  service.session = random_hex();
  if (service.token.empty() || service.session.empty()) {
    r_error = "Could not obtain OS randomness";
    return false;
  }
  /* mkdtemp creates a private directory atomically, without following a predictable symlink. */
  char directory[] = "/tmp/blender-agent-XXXXXX";
  if (!mkdtemp(directory)) {
    r_error = "Could not create private runtime directory";
    return false;
  }
  service.directory = directory;
  service.endpoint = service.directory + "/bridge.sock";
  service.listener = socket(AF_UNIX, SOCK_STREAM | SOCK_CLOEXEC | SOCK_NONBLOCK, 0);
  service.wake_fd = eventfd(0, EFD_CLOEXEC | EFD_NONBLOCK);
  sockaddr_un address{};
  address.sun_family = AF_UNIX;
  memcpy(address.sun_path, service.endpoint.c_str(), service.endpoint.size() + 1);
  if (service.listener < 0 || service.wake_fd < 0 ||
      bind(service.listener, reinterpret_cast<sockaddr *>(&address), sizeof(address)) != 0 ||
      chmod(service.endpoint.c_str(), 0600) != 0 || listen(service.listener, 8) != 0)
  {
    r_error = "Could not bind private Unix socket";
    service.shutdown();
    return false;
  }
  auto descriptor = std::make_shared<DictionaryValue>();
  descriptor->append_int("protocol_major", 1);
  descriptor->append_int("pid", getpid());
  descriptor->append_str("session_id", service.session);
  descriptor->append_str("endpoint", service.endpoint);
  descriptor->append_str("token", service.token);
  const std::string path = service.directory + "/session.json";
  const int fd = open(path.c_str(), O_WRONLY | O_CREAT | O_EXCL | O_CLOEXEC, 0600);
  const std::string data = encode(*descriptor);
  if (fd < 0 || write(fd, data.data(), data.size()) != ssize_t(data.size())) {
    if (fd >= 0) {
      close(fd);
    }
    r_error = "Could not write private session descriptor";
    service.shutdown();
    return false;
  }
  close(fd);
  service.running = true;
  service.thread = std::thread([] { service.io_loop(); });
  return true;
}

void stop()
{
  service.shutdown();
}

bool is_running()
{
  return service.running;
}

bool set_journal(const std::string &path, const size_t max_bytes, std::string &r_error)
{
  std::lock_guard lock(service.mutex);
  if (!service.running || service.journal_fd >= 0 || path.empty() || path.front() != '/' ||
      max_bytes < 4096 || max_bytes > 16 * 1024 * 1024)
  {
    r_error = "Journal requires a running session and a new absolute output path";
    return false;
  }
  const int fd = open(path.c_str(), O_WRONLY | O_CREAT | O_EXCL | O_CLOEXEC | O_NOFOLLOW, 0600);
  if (fd < 0) {
    r_error = "Could not create private journal";
    return false;
  }
  service.journal_fd = fd;
  service.journal_path = path;
  service.journal_limit = max_bytes;
  service.journal_last_sync = Clock::now();
  service.emit("session.started");
  for (auto &[id, job] : service.jobs) {
    service.emit("job.snapshot", &job);
  }
  return true;
}

void update_metadata(const std::string &json)
{
  Json metadata = parse(json);
  if (metadata && metadata->as_dictionary_value()) {
    std::lock_guard lock(service.mutex);
    service.metadata = std::move(metadata);
  }
}

std::string info_json()
{
  std::lock_guard lock(service.mutex);
  auto result = std::make_shared<DictionaryValue>();
  result->append_bool("running", service.running);
  result->append_str("session_id", service.session);
  result->append_str("descriptor",
                     service.directory.empty() ? "" : service.directory + "/session.json");
  result->append_int("jobs", service.jobs.size());
  result->append_int("pending", service.pending.size());
  result->append_int("clients", service.clients.size());
  result->append_int("event_cursor", service.event_cursor);
  result->append_str("journal", service.journal_path);
  result->append_bool("journal_healthy",
                      service.journal_fd >= 0 && !service.journal_failed && !service.journal_full);
  result->append_int("journal_limit_bytes", service.journal_limit);
  result->append_str("journal_state",
                     service.journal_failed  ? "io_error" :
                     service.journal_full    ? "limit_reached" :
                     service.journal_fd >= 0 ? "recording" :
                                               "disabled");
  return encode(*result);
}

void poll(const Dispatch &dispatch)
{
  const auto begin = Clock::now();
  while (service.running && Clock::now() - begin < std::chrono::milliseconds(4)) {
    Pending pending;
    {
      std::lock_guard lock(service.mutex);
      if (service.pending.empty()) {
        return;
      }
      pending = std::move(service.pending.front());
      service.pending.pop_front();
      if (!pending.job_id.empty()) {
        Job &job = service.jobs.at(pending.job_id);
        if (job.cancel) {
          job.state = "cancelled";
          job.phase = "cancelled";
          service.emit("job.cancelled", &job);
          continue;
        }
        if (!pending.control) {
          job.state = "running";
          job.phase = "running";
        }
        job.stage_started = Clock::now();
        job.stage_active = true;
        service.emit("job.started", &job);
      }
      else if (service.clients.find(pending.client) == service.clients.end()) {
        continue;
      }
    }
    Json outcome = parse(dispatch(encode(*pending.request)));
    if (!outcome || !outcome->as_dictionary_value()) {
      outcome = error(field(*pending.request->as_dictionary_value(), "id"),
                      -32603,
                      "INTERNAL_ERROR",
                      "Dispatcher returned an invalid response");
    }
    if (!pending.job_id.empty()) {
      if (outcome->as_dictionary_value()->lookup_bool("_agent_deferred").value_or(false)) {
        continue;
      }
      std::string state = outcome->as_dictionary_value()->lookup("error") ? "failed" : "succeeded";
      const DictionaryValue *detail = outcome->as_dictionary_value()->lookup_dict("error");
      const DictionaryValue *data = detail ? detail->lookup_dict("data") : nullptr;
      if (data && string_field(*data, "kind") == "CANCELLED") {
        state = "cancelled";
      }
      else if (data &&
               (string_field(*data, "kind") == "REVISION_CONFLICT" ||
                string_field(*data, "kind") == "STALE_HANDLE") &&
               pending.control)
      {
        state = "conflict";
      }
      else if (pending.control &&
               string_field(*pending.request->as_dictionary_value(), "method") == "job.discard" &&
               state == "succeeded")
      {
        state = "discarded";
      }
      finish(pending.job_id, state, encode(*outcome));
    }
    else {
      std::lock_guard lock(service.mutex);
      auto found = service.clients.find(pending.client);
      if (found != service.clients.end()) {
        found->second.outstanding--;
        service.send(found->second, *outcome);
      }
    }
  }
}

bool cancellation_requested(const std::string &job_id)
{
  std::lock_guard lock(service.mutex);
  const auto found = service.jobs.find(job_id);
  return found != service.jobs.end() && found->second.cancel;
}

bool release_result(const std::string &job_id)
{
  std::lock_guard lock(service.mutex);
  const auto found = service.jobs.find(job_id);
  if (found == service.jobs.end()) {
    return false;
  }
  Job &job = found->second;
  if (job.state != "succeeded" && job.state != "failed" && job.state != "cancelled" &&
      job.state != "conflict" && job.state != "discarded" && job.state != "interrupted")
  {
    return false;
  }
  job.resources_released = true;
  job.outcome.reset();
  service.emit("job.resources_released", &job);
  return true;
}

void progress(const std::string &job_id, const std::string &phase, const double fraction)
{
  std::lock_guard lock(service.mutex);
  const auto found = service.jobs.find(job_id);
  if (found != service.jobs.end()) {
    const std::string bounded_phase = bounded_utf8(phase, 128);
    const bool phase_changed = found->second.phase != bounded_phase;
    found->second.phase = bounded_phase;
    found->second.progress_known = std::isfinite(fraction);
    if (found->second.progress_known) {
      found->second.fraction = std::clamp(fraction, 0.0, 1.0);
    }
    if (phase_changed ||
        Clock::now() - found->second.last_progress_event > std::chrono::milliseconds(100))
    {
      found->second.last_progress_event = Clock::now();
      service.emit("job.progress", &found->second);
    }
  }
}

bool finish(const std::string &job_id, const std::string &state, const std::string &response)
{
  if (state != "ready" && state != "succeeded" && state != "failed" && state != "cancelled" &&
      state != "conflict" && state != "discarded" && state != "interrupted")
  {
    return false;
  }
  Json outcome = parse(response);
  if (!outcome || !outcome->as_dictionary_value()) {
    return false;
  }
  std::lock_guard lock(service.mutex);
  const auto found = service.jobs.find(job_id);
  if (found == service.jobs.end()) {
    return false;
  }
  Job &job = found->second;
  if (job.stage_active) {
    job.elapsed_ms +=
        std::chrono::duration<double, std::milli>(Clock::now() - job.stage_started).count();
    job.stage_active = false;
  }
  job.state = job.cancel && state == "ready" ? "cancelled" : state;
  job.phase = job.state;
  job.outcome = std::move(outcome);
  if (job.state == "succeeded" || job.state == "ready") {
    job.fraction = 1.0;
    job.progress_known = true;
  }
  service.emit("job.finished", &job);
  if (job.state == "ready" && job.auto_apply) {
    if (service.pending.size() >= max_pending) {
      job.state = "failed";
      job.phase = "auto_apply_queue_full";
      job.outcome = error(field(*job.outcome->as_dictionary_value(), "id"),
                          -32001,
                          "BUSY",
                          "Auto-apply queue is full");
      service.emit("job.finished", &job);
      return true;
    }
    auto request = std::make_shared<DictionaryValue>();
    request->append_str("jsonrpc", "2.0");
    request->append_str("id", "auto-apply");
    request->append_str("method", "job.apply");
    request->append_dict("params")->append_str("job_id", job_id);
    job.state = "applying";
    job.phase = "applying";
    job.progress_known = false;
    service.pending.push_back({0, request, job_id, true});
    service.emit("job.applying", &job);
  }
  return true;
}

std::string request_json(const std::string &request)
{
  Json value = parse(request);
  const DictionaryValue *dict = value ? value->as_dictionary_value() : nullptr;
  const std::string method = dict ? string_field(*dict, "method") : "";
  if (method != "job.submit" && method != "job.status" && method != "job.cancel" &&
      method != "job.apply" && method != "job.discard" && method != "job.list" &&
      method != "events.poll")
  {
    return encode(*error(dict ? field(*dict, "id") : std::make_shared<NullValue>(),
                         -32601,
                         "METHOD_NOT_FOUND",
                         "In-process request supports queued jobs/status/control only"));
  }
  std::lock_guard lock(service.mutex);
  if (!service.running) {
    return encode(*error(field(*dict, "id"), -32000, "NOT_RUNNING", "Agent service is stopped"));
  }
  Client client;
  client.authenticated = true;
  client.identity = "in-process";
  service.receive(client, request);
  if (client.output.size() < 4) {
    return encode(
        *error(field(*dict, "id"), -32603, "INTERNAL_ERROR", "Missing acceptance result"));
  }
  return client.output.substr(4);
}

}  // namespace blender::agent_bridge
