# Vendored Python wheels

Put downloaded Linux CPython 3.12 wheels for the preview/processing server here.

Populate this directory from the repository root:

```powershell
.\resource_processing_server\docker\vendor\fetch_pip_vendor.ps1
```

When this directory contains wheel files, the Dockerfiles install Python
dependencies with `pip --no-index --find-links` and do not contact PyPI.
