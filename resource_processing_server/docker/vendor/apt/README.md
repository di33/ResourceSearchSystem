# Vendored apt packages

Put pre-downloaded `.deb` packages for the resource processing server image in
this directory.

Use `..\fetch_apt_vendor.ps1` to populate it with packages that match
`python:3.12-slim`. The Dockerfile installs from this directory first; when it is
empty, it falls back to online `apt-get` unless `RP_APT_VENDOR_MODE=required`.
