/* SPDX-FileCopyrightText: 2026 Blender Authors
 *
 * SPDX-License-Identifier: GPL-2.0-or-later */

#include <Python.h>

#include "AGENT_service.hh"
#include "BLI_memory_utils.hh"
#include "BLI_threads.hh"
#include "BLI_timer.hh"
#include "BPY_extern.hh"
#include "bpy_agent_bridge.hh"

#include <csignal>
#include <sys/prctl.h>
#include <unistd.h>

namespace blender {
namespace {

static char timer_identity;
static bool dispatching = false;

void shutdown_adapter()
{
  if (!Py_IsInitialized()) {
    return;
  }
  PyGILState_STATE gil = PyGILState_Ensure();
  PyObject *module = PyDict_GetItemString(PyImport_GetModuleDict(), "bl_agent.runtime");
  if (module) {
    PyObject *result = PyObject_CallMethod(module, "shutdown", nullptr);
    Py_XDECREF(result);
    if (PyErr_Occurred()) {
      PyErr_PrintEx(0);
    }
  }
  PyGILState_Release(gil);
}

bool main_thread_check()
{
  if (!BLI_thread_is_main()) {
    PyErr_SetString(PyExc_RuntimeError, "Agent scene dispatch requires Blender's main thread");
    return false;
  }
  return true;
}

void dispatch_pending()
{
  if (dispatching) {
    return;
  }
  dispatching = true;
  BLI_SCOPED_DEFER([]() { dispatching = false; });
  /* Poll only owned subprocesses; this never waits for a running worker. */
  {
    PyGILState_STATE gil = PyGILState_Ensure();
    PyObject *module = PyImport_ImportModule("bl_agent.runtime");
    PyObject *result = module ? PyObject_CallMethod(module, "advance_jobs", nullptr) : nullptr;
    Py_XDECREF(result);
    Py_XDECREF(module);
    if (PyErr_Occurred()) {
      PyErr_PrintEx(0);
    }
    PyGILState_Release(gil);
  }
  agent_bridge::poll([](const std::string &request) -> std::string {
    PyGILState_STATE gil = PyGILState_Ensure();
    std::string response;
    PyObject *module = PyImport_ImportModule("bl_agent.runtime");
    PyObject *result = module ?
                           PyObject_CallMethod(module, "dispatch_json", "s", request.c_str()) :
                           nullptr;
    if (result) {
      const char *text = PyUnicode_AsUTF8(result);
      if (text) {
        response = text;
      }
      Py_DECREF(result);
    }
    if (PyErr_Occurred()) {
      PyErr_PrintEx(0);
    }
    Py_XDECREF(module);
    PyGILState_Release(gil);
    return response;
  });
}

double timer_poll(uintptr_t /*uuid*/, void * /*user_data*/)
{
  if (dispatching) {
    return 0.02;
  }
  if (!agent_bridge::is_running()) {
    shutdown_adapter();
    agent_bridge::stop();
    return -1;
  }
  dispatch_pending();
  return 0.02;
}

PyObject *py_start(PyObject * /*self*/, PyObject * /*args*/)
{
  if (!main_thread_check()) {
    return nullptr;
  }
  if (dispatching) {
    PyErr_SetString(PyExc_RuntimeError, "Cannot start the bridge during scene dispatch");
    return nullptr;
  }
  std::string error;
  if (!agent_bridge::start(error)) {
    PyErr_SetString(PyExc_RuntimeError, error.c_str());
    return nullptr;
  }
  const uintptr_t timer = reinterpret_cast<uintptr_t>(&timer_identity);
  if (!BLI_timer_is_registered(timer)) {
    BLI_timer_register(timer, timer_poll, nullptr, nullptr, 0.02, true);
  }
  Py_RETURN_NONE;
}

PyObject *py_stop(PyObject * /*self*/, PyObject * /*args*/)
{
  if (!main_thread_check()) {
    return nullptr;
  }
  if (dispatching) {
    PyErr_SetString(PyExc_RuntimeError,
                    "Cannot stop the bridge recursively; cancel or stop from another client");
    return nullptr;
  }
  shutdown_adapter();
  agent_bridge::stop();
  const uintptr_t timer = reinterpret_cast<uintptr_t>(&timer_identity);
  if (BLI_timer_is_registered(timer)) {
    BLI_timer_unregister(timer);
  }
  Py_RETURN_NONE;
}

PyObject *py_info(PyObject * /*self*/, PyObject * /*args*/)
{
  return PyUnicode_FromString(agent_bridge::info_json().c_str());
}

PyObject *py_metadata(PyObject * /*self*/, PyObject *args)
{
  const char *json;
  if (!main_thread_check() || !PyArg_ParseTuple(args, "s:update_metadata", &json)) {
    return nullptr;
  }
  agent_bridge::update_metadata(json);
  Py_RETURN_NONE;
}

PyObject *py_poll(PyObject * /*self*/, PyObject * /*args*/)
{
  if (!main_thread_check()) {
    return nullptr;
  }
  if (dispatching) {
    PyErr_SetString(PyExc_RuntimeError, "Cannot recursively dispatch agent operations");
    return nullptr;
  }
  dispatch_pending();
  Py_RETURN_NONE;
}

PyObject *py_cancelled(PyObject * /*self*/, PyObject *args)
{
  const char *job_id;
  if (!PyArg_ParseTuple(args, "s:cancelled", &job_id)) {
    return nullptr;
  }
  return PyBool_FromLong(agent_bridge::cancellation_requested(job_id));
}

PyObject *py_progress(PyObject * /*self*/, PyObject *args)
{
  const char *job_id, *phase;
  double fraction;
  if (!PyArg_ParseTuple(args, "ssd:progress", &job_id, &phase, &fraction)) {
    return nullptr;
  }
  agent_bridge::progress(job_id, phase, fraction);
  Py_RETURN_NONE;
}

PyObject *py_finish(PyObject * /*self*/, PyObject *args)
{
  const char *job_id, *state, *response;
  if (!main_thread_check() || !PyArg_ParseTuple(args, "sss:finish", &job_id, &state, &response)) {
    return nullptr;
  }
  return PyBool_FromLong(agent_bridge::finish(job_id, state, response));
}

PyObject *py_worker_parent_watch(PyObject * /*self*/, PyObject *args)
{
  long expected_parent;
  if (!PyArg_ParseTuple(args, "l:worker_parent_watch", &expected_parent)) {
    return nullptr;
  }
  if (expected_parent <= 1 || prctl(PR_SET_PDEATHSIG, SIGTERM) != 0 ||
      getppid() != expected_parent)
  {
    PyErr_SetString(PyExc_RuntimeError, "Worker parent is gone or death-signal setup failed");
    return nullptr;
  }
  Py_RETURN_NONE;
}

PyObject *py_request(PyObject * /*self*/, PyObject *args)
{
  const char *request;
  if (!PyArg_ParseTuple(args, "s:request_json", &request)) {
    return nullptr;
  }
  return PyUnicode_FromString(agent_bridge::request_json(request).c_str());
}

PyObject *py_ensure_main_thread(PyObject * /*self*/, PyObject * /*args*/)
{
  if (!main_thread_check()) {
    return nullptr;
  }
  Py_RETURN_NONE;
}

PyObject *py_set_journal(PyObject * /*self*/, PyObject *args)
{
  const char *path;
  unsigned long long max_bytes = 16 * 1024 * 1024;
  if (!main_thread_check() || !PyArg_ParseTuple(args, "s|K:set_journal", &path, &max_bytes)) {
    return nullptr;
  }
  std::string error;
  if (!agent_bridge::set_journal(path, size_t(max_bytes), error)) {
    PyErr_SetString(PyExc_RuntimeError, error.c_str());
    return nullptr;
  }
  Py_RETURN_NONE;
}

PyObject *py_release_result(PyObject * /*self*/, PyObject *args)
{
  const char *job_id;
  if (!main_thread_check() || !PyArg_ParseTuple(args, "s:release_result", &job_id)) {
    return nullptr;
  }
  return PyBool_FromLong(agent_bridge::release_result(job_id));
}

PyMethodDef methods[] = {
    {"start", py_start, METH_NOARGS, "Start the native transport (main thread only)."},
    {"stop", py_stop, METH_NOARGS, "Stop and join the native transport."},
    {"info_json", py_info, METH_NOARGS, "Return native service metadata without credentials."},
    {"update_metadata",
     py_metadata,
     METH_VARARGS,
     "Cache main-thread discovery metadata for reconnects."},
    {"poll", py_poll, METH_NOARGS, "Dispatch queued work; useful in background test loops."},
    {"cancelled", py_cancelled, METH_VARARGS, "Read a job's cooperative cancellation flag."},
    {"progress", py_progress, METH_VARARGS, "Report a job's progress."},
    {"finish", py_finish, METH_VARARGS, "Complete a deferred stage (main thread only)."},
    {"set_journal", py_set_journal, METH_VARARGS, "Create a private session diagnostic journal."},
    {"release_result",
     py_release_result,
     METH_VARARGS,
     "Release terminal result references without forgetting its operation key."},
    {"worker_parent_watch",
     py_worker_parent_watch,
     METH_VARARGS,
     "Terminate this worker if its parent exits."},
    {"request_json",
     py_request,
     METH_VARARGS,
     "Submit/query/control native jobs without a socket."},
    {"ensure_main_thread",
     py_ensure_main_thread,
     METH_NOARGS,
     "Reject scene access from a Python worker thread."},
    {nullptr, nullptr, 0, nullptr},
};

PyModuleDef module_def = {
    PyModuleDef_HEAD_INIT, "_bpy.agent_bridge", "Native local agent service.", 0, methods};
}  // namespace

PyObject *BPY_agent_bridge_module()
{
  return PyModule_Create(&module_def);
}

void BPY_agent_bridge_shutdown()
{
  shutdown_adapter();
}
}  // namespace blender
