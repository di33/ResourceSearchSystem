# Vendored npm cache

Put an npm cache for `Tools/spine_preview/package-lock.json` here.

Populate this directory from the repository root:

```powershell
.\resource_processing_server\docker\vendor\fetch_npm_vendor.ps1
```

When this directory contains `_cacache`, the Dockerfiles run `npm ci --offline`
and do not contact the npm registry.
