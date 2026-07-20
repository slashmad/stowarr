# Security policy

Do not open a public issue for suspected vulnerabilities or exposed secrets.
Use GitHub's private vulnerability reporting for `slashmad/stowarr` when it is
available.

Stowarr deliberately separates its WebUI and API containers. Only the API
container should receive service credentials, persistent state, and media
mounts. Keep the API listener bound to localhost or a trusted private network,
set a unique `STOWARR_API_TOKEN`, and review every confirmation plan before
enabling write access.
