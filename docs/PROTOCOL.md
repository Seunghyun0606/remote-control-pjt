# Runner Protocol

R1 uses an outbound WebSocket connection from each remote Runner to the Controller.

Endpoint: `/ws/runner`

Protocol messages use an envelope containing `protocol_version`, `type`, `id`, `timestamp`, and `payload`. R1 supports protocol version 1 and rejects unknown versions.

The first Runner message is `HOST_REGISTER` with host id, name, OS and capabilities. The Runner then sends `HEARTBEAT` periodically with its running execution ids.

Controller to Runner:

- `JOB_START`
- `JOB_CANCEL`

Reserved for later:

- `JOB_PAUSE`
- `JOB_RESUME`
- `JOB_STEER`

Runner to Controller:

- `JOB_ACCEPTED`
- `JOB_PROGRESS`
- `JOB_RESULT`
- `JOB_ERROR`
- `SESSION_STARTED`

`JOB_START` carries a generated execution id, project id, instruction and a working directory selected from the Controller-side project registry.
