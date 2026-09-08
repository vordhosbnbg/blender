# Building with Linux system dependencies

The `blender_linux_system.cmake` cache preset uses distribution libraries and the
system Python installation. It discovers the Python major/minor version from
`/usr/bin/python3`, keeps Blender's Python API enabled, and disables copying Python
or its packages into the build. Blender's precompiled library download is not
needed.

The preset also resolves package configuration symlinks, so packages loaded
through Arch Linux's `/lib` symlink find their headers under `/usr/include`.

System Vulkan configuration checks for the separately packaged SPIR-V headers,
so a missing `spirv-headers` package is reported before compilation starts.

## Fork and upstream sync

Local development uses `main`, which tracks `origin/main` in the personal fork:

- `origin`: `git@github.com:vordhosbnbg/blender.git`
- `upstream`: `git@github.com:blender/blender.git`

Git LFS retains the existing Blender asset-server URL. Local lock verification
for that endpoint is disabled to avoid its authentication prompt during pushes
to the fork:

```sh
git config lfs.https://projects.blender.org/blender/blender.git/info/lfs.locksverify false
```

With local changes committed, sync from the Blender source directory:

```sh
git switch main
git pull --ff-only
git fetch upstream
git rebase upstream/main
# Resolve any conflicts, rebuild, and run checks relevant to the changes.
git push --force-with-lease origin main
```

Keep fork-specific commits grouped by purpose in a linear series on top of
upstream. Rebasing rewrites their commit IDs, so publish with `--force-with-lease`.
The configure and build commands below keep the existing system-library profile.

## Configure and build

Run from the Blender source directory. The GPU settings below match the local
RTX 3080 Ti, CUDA toolkit, GCC 15 CUDA host compiler, and OptiX 8 headers:

```sh
cmake -G Ninja \
  -C build_files/cmake/config/blender_linux_system.cmake \
  -DOPTIX_ROOT_DIR="$HOME/.local/opt/optix-8.0.0" \
  -DCUDA_HOST_COMPILER=/usr/bin/g++-15 \
  -DWITH_CYCLES_DEVICE_OPTIX=ON \
  -DWITH_CYCLES_CUDA_BINARIES=ON \
  -DCYCLES_CUDA_BINARIES_ARCH=sm_86 \
  -DCMAKE_BUILD_TYPE=Release \
  -DNINJA_MAX_NUM_PARALLEL_COMPILE_HEAVY_JOBS=2 \
  -S . -B build-system

cmake --build build-system --parallel 8 --target install
./build-system/bin/blender
```

The install target assembles Blender's executable, scripts, and assets in
`build-system/bin`. It uses the system Python runtime and native library packages
at run time. Changes to those packages' binary interfaces require rebuilding
Blender.

Use a new build directory or add `--fresh` to the complete configure command when
switching dependency sources or upgrading Python. For a system Python in another
location, pass `-DPYTHON_EXECUTABLE=/path/to/python3` before `-C`.

## Arch Linux dependencies

In addition to the existing compiler, CMake, Ninja, CUDA, Python, NumPy, Requests,
Zstandard, and desktop development packages, this build uses:

```text
alembic ceres-solver draco eigen embree libharu libspnav manifold materialx meshoptimizer
opencolorio openimagedenoise openimageio openpgl openshadinglanguage opensubdiv
openvdb openxr potrace pystring python-cattrs spirv-headers usd
```

CMake reports missing optional libraries during configuration. Meshoptimizer is
available from the AUR rather than the official Arch repositories. This machine
uses a library-only package built from the meshoptimizer 1.2 release and source
checksum in the reviewed AUR recipe. Its package archive is
`/home/packages/meshoptimizer-1.2-1-x86_64.pkg.tar.zst`; the local build recipe is in
`build-system/system-packages/meshoptimizer/PKGBUILD`. Without this library,
CMake disables glTF meshoptimizer compression automatically.

On 2026-11-03, Arch USD 26.11.1 omitted `pxr/imaging/glf/drawTarget.h`; its unused
include in the Hydra viewport was removed. With oneTBB 2023.2.0, the Grease Pencil
build modifier's comparison for sorting strokes must accept `const Pair &` arguments.
These local compatibility corrections retain Hydra, USD, and TBB support.

## Validation

Verified on 2026-11-03 with Blender 5.4.0 Alpha, Arch Python 3.14.9, NumPy 2.5.4,
FFmpeg 9.0.2, CUDA 13.4.3, GCC 16.2.1, and an NVIDIA GeForce RTX 3080 Ti:

- Full Release build and local install completed.
- All 312 tests in `bl_pyapi_prop.py`, `bl_pyapi_prop_array.py`,
  `bl_pyapi_idprop.py`, and `bl_pyapi_mathutils.py` passed.
- A Python property group registered and used Python 3.14's deferred annotations.
- CPU, CUDA, and OptiX each rendered a nonblank 64 x 64 PNG.
- USD export, AAC audio export, and a meshoptimizer-compressed glTF export/import
  completed successfully.
- `ldd build-system/bin/blender` resolved Python and external shared libraries
  from the system; no libraries were missing.
- Hydra Storm rendered a nonblank 64 x 64 PNG and exited successfully. System
  USD 26.11.1 logged OpenGL `invalid enum` diagnostics during this check.

The local runtime probe and results are retained in
`build-system/system_runtime_probe.py` and `build-system/runtime-results`.
Tests need normal access to the graphics drivers and desktop audio services;
the restricted agent sandbox blocks those services and can stall audio shutdown.

## Existing checkout cleanup

The local checkout contained 25 untracked shader compatibility headers in their
old locations under `source/blender/gpu/shaders`. They shadowed the tracked headers
in `shaders/compat` and broke Workbench's C++ shader validation with missing
`rayQueryEXT` declarations. These were initially archived under
`.source-backup/20260908`. The current tracked headers already provide the required
API.

On 2026-09-08, a further audit verified that all 223 remaining untracked source
files exactly matched historical Git versions. They were obsolete headers,
shaders, build scripts, and other sources whose paths had been removed or renamed
upstream. At the user's request, those 223 files and the 25 archived headers were
deleted after rechecking their checksums and Git blobs. The backup directory and
its manifest were also removed. The ignored `build/` and `build-system/`
directories, including the Blender executable and rendered artwork, were
preserved; no tracked source files were changed.
