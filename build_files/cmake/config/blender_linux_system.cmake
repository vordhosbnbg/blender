# SPDX-FileCopyrightText: 2026 Blender Authors
#
# SPDX-License-Identifier: GPL-2.0-or-later

# Build against Linux distribution libraries and Python, without bundling Python.
# Example usage from the source directory:
#   cmake -C build_files/cmake/config/blender_linux_system.cmake -S . -B build-system
# Use a fresh build directory when switching from precompiled libraries.

set(WITH_LIBS_PRECOMPILED OFF CACHE BOOL "" FORCE)
set(WITH_STATIC_LIBS OFF CACHE BOOL "" FORCE)
set(WITH_SYSTEM_GFLAGS ON CACHE BOOL "" FORCE)
set(WITH_SYSTEM_GLOG ON CACHE BOOL "" FORCE)
set(WITH_PYTHON_INSTALL OFF CACHE BOOL "" FORCE)
set(WITH_PYTHON_INSTALL_NUMPY OFF CACHE BOOL "" FORCE)
set(WITH_PYTHON_INSTALL_REQUESTS OFF CACHE BOOL "" FORCE)
set(WITH_PYTHON_INSTALL_ZSTANDARD OFF CACHE BOOL "" FORCE)

# Packages such as OpenImageIO compute include paths relative to their CMake
# files. Resolve /lib -> /usr/lib before loading these package configurations.
set(CMAKE_FIND_PACKAGE_RESOLVE_SYMLINKS ON CACHE BOOL "" FORCE)

# Avoid selecting an activated virtual environment. PYTHON_EXECUTABLE may be
# supplied explicitly for distributions with Python in a different location.
find_program(PYTHON_EXECUTABLE
  NAMES python3
  PATHS /usr/bin /bin
  NO_DEFAULT_PATH
  REQUIRED
)
execute_process(
  COMMAND "${PYTHON_EXECUTABLE}" -c
    "import sys; print('%d.%d' % sys.version_info[:2]); print(sys.base_prefix)"
  OUTPUT_VARIABLE _system_python_config
  OUTPUT_STRIP_TRAILING_WHITESPACE
  COMMAND_ERROR_IS_FATAL ANY
)
string(REPLACE "\n" ";" _system_python_config "${_system_python_config}")
list(GET _system_python_config 0 _system_python_version)
list(GET _system_python_config 1 _system_python_root)
set(PYTHON_VERSION "${_system_python_version}" CACHE STRING "Python Version (major and minor only)" FORCE)
set(PYTHON_ROOT_DIR "${_system_python_root}" CACHE PATH "Python installation prefix" FORCE)
unset(_system_python_config)
unset(_system_python_version)
unset(_system_python_root)
