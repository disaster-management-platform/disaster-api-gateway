# API Gateway

ASP.NET Core 10 / YARP. Explicit routes and bounded proxy traffic.

### Container image

Build from the repository root, using BuildKit:

```sh
docker build -t disaster-api-gateway:local .
```

The runtime runs as the image's non-root application UID, listens on 8080, and receives configuration and trust roots externally. `SDK_IMAGE` and `RUNTIME_IMAGE` can be set to approved image digests for repeatable releases. Version tags are pinned; use approved digests for immutable releases. Configure HTTPS at the trusted ingress and preserve HTTPS issuer/downstream validation; private CA roots must be installed through the deployment trust configuration, never bypassed. Publish ports only as required. No build or normal startup applies database migrations. Supply runtime credentials using the deployment secret store.
