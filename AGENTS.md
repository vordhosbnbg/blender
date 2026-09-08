# Local Blender Development

## Workflow

- Work on `main`, tracking `origin/main` in `vordhosbnbg/blender`. Rebase onto
  `upstream/main` from `blender/blender` to sync while retaining local changes.
- Inspect Git status before editing. Preserve unrelated changes and untracked
  files; stage only task files. Avoid broad cleanup commands.
- Keep corrections small and follow nearby code, `.editorconfig`, and
  `.clang-format`. Prefer build configuration fixes when the existing source
  supports the installed dependency.
- Keep durable Blender build and debugging documentation in this checkout.
  Modify `~/arch-system-maintenance` only when explicitly requested.

## Build

Follow [the build guide](doc/building_system_linux.md) for configuration commands,
dependencies, and host GPU settings.

- Use `build_files/cmake/config/blender_linux_system.cmake` with system libraries
  and `/usr/bin/python3`. Keep embedded Python and Blender's Python API enabled.
- Do not use virtual environments, precompiled/static dependency bundles, or
  bundled Python/packages for this build.
- Preserve CUDA and OptiX support.
- Build in `build-system`; use the `install` target to assemble the local
  executable, scripts, and assets under `build-system/bin`.
- Use at most eight parallel build jobs and two heavy compile jobs.

## Validation

- Run checks relevant to the change and verify their results and exit status.
  For Python compatibility or dependency changes, run `bl_pyapi_prop`,
  `bl_pyapi_prop_array`, `bl_pyapi_idprop`, and `bl_pyapi_mathutils` from
  `tests/python`. For GPU or native-library changes, verify rendering/export.
- Documentation-only changes do not require rebuilding or running Blender tests.
- Before committing, inspect the exact staged diff and run
  `git diff --cached --check`.
