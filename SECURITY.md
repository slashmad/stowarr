# Security policy

Do not open a public issue for suspected vulnerabilities or exposed secrets.
Use GitHub's private vulnerability reporting for `slashmad/stowarr` when it is
available.

Stowarr deliberately separates its WebUI and API containers. Only the API
container should receive service credentials, persistent state, and media
mounts. Keep the API listener bound to localhost or a trusted private network,
use the generated unique API key or set a unique `STOWARR_API_TOKEN`, and review
every confirmation plan before enabling write access. Keep Forms authentication
enabled unless Stowarr is exclusively reachable through an authentication
proxy that replaces the configured trusted-user header. Never expose External
authentication through a path that bypasses that proxy.
