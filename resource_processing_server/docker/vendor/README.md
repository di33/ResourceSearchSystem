# Resource processing server vendored tools

This directory is for deployment-time dependencies that should be prepared
before building the resource processing server and preview renderer images.

The Dockerfiles support three vendor layers:

- `apt/`: Debian `.deb` packages for preview tools such as Chromium, FFmpeg,
  Node, npm, fonts, and their shared-library dependencies.
- `pip/`: Linux CPython 3.12 wheels for Python requirements.
- `npm/`: npm cache for `Tools/spine_preview/package-lock.json`.

Populate the three layers from the repository root:

```powershell
.\resource_processing_server\docker\vendor\fetch_apt_vendor.ps1
.\resource_processing_server\docker\vendor\fetch_pip_vendor.ps1
.\resource_processing_server\docker\vendor\fetch_npm_vendor.ps1
```

Include Blender only when the processing server should render FBX previews with
Blender instead of placeholder GIFs:

```powershell
.\resource_processing_server\docker\vendor\fetch_apt_vendor.ps1 -IncludeBlender
```

The apt script does not require Docker. It downloads one `.deb` at a time,
verifies SHA256 checksums, keeps partial downloads as `.partial`, and writes
`apt/manifest.json` only after the full dependency set has been downloaded.
The Dockerfiles use apt vendor packages only when that manifest exists.

For an offline build, set `RP_APT_VENDOR_MODE=required` so the image build fails
if the apt vendor layer has not been prepared completely:

```powershell
cd resource_processing_server
$env:RP_APT_VENDOR_MODE = "required"
docker compose build
```

The apt package list is intentionally kept in `../apt-packages.txt` and
`../apt-packages.blender.txt` so the Dockerfile and the prefetch script use the
same dependency boundary.
