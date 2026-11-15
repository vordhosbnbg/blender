# SPDX-FileCopyrightText: 2026 Blender Authors
#
# SPDX-License-Identifier: GPL-2.0-or-later

set(INC PUBLIC .)
set(INC_SYS)
set(SRC
  intern/agent_service.cc
  AGENT_service.hh
)
set(LIB PRIVATE bf::blenlib PRIVATE bf::intern::guardedalloc)

blender_add_lib(bf_agent_bridge "${SRC}" "${INC}" "${INC_SYS}" "${LIB}")
add_library(bf::agent_bridge ALIAS bf_agent_bridge)
