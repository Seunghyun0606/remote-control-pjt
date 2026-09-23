# R6 Manual Smoke Test

Use a test Slack workspace and a disposable project for the Slack portion.

## 1. Regression baseline

Start the Controller and configured Runners.

Verify:

```text
/hosts
/projects
```

Telegram behavior from R0-R5 must remain unchanged.

## 2. Slack configuration

Create/install a Slack app with:

- Socket Mode enabled
- App-Level Token with `connections:write`
- Bot scopes `chat:write`, `im:history`, `im:write`
- Bot event subscription `message.im`
- App Home Messages tab enabled if used

Controller environment:

```dotenv
REMOTE_CONTROL_SLACK_ENABLED=true
SLACK_BOT_TOKEN=xoxb-...
SLACK_APP_TOKEN=xapp-...
SLACK_ALLOWED_USER_IDS=<your Slack user id>
```

Restart the Controller.

Expected: Slack connects without exposing a Slack HTTP webhook endpoint.

## 3. Slack authorization

From an allowlisted user DM:

```text
/hosts
```

Expected: Host list.

From a non-allowlisted user:

Expected: permission denied and no Job creation.

## 4. Slack Job

DM:

```text
/run <project>
```

Expected:

- immediate Slack response with Job id
- Job requested_by_channel = slack
- progress/final notifications return to that Slack user
- Telegram does not receive the Slack Job notifications

Run a Telegram Job at the same time and verify its notifications still go to Telegram.

## 5. Slack Human Gate

Trigger a disposable Human Gate.

Expected Slack message:

- one button per option
- Details
- Reject

Select an option.

Expected:

- Approval owner check succeeds only for the originating user
- Job resumes through the existing R3 session flow

## 6. Web Dashboard

With:

```dotenv
REMOTE_CONTROL_WEB_UI_ENABLED=true
```

open:

```text
http://127.0.0.1:8787/ui
```

Expected:

- projects metric
- online Host count
- active/recent Jobs
- task id for Project OS Jobs
- pending Human Gates
- refresh without page reload

The UI must not display action buttons for run/steer/stop/approval.

## 7. Disable Web UI

Set:

```dotenv
REMOTE_CONTROL_WEB_UI_ENABLED=false
```

Restart Controller.

Expected:

- `GET /ui` = 404
- `GET /dashboard` still returns JSON snapshot

## 8. Network boundary

Confirm the default Controller bind is still:

```text
127.0.0.1:8787
```

Slack Socket Mode and Desktop Runner both initiate outbound connections.

Do not expose the FastAPI port publicly without an authenticated reverse proxy/private network.

## Automated coverage

CI covers:

- Slack approval payload validation
- Slack allowlist parsing
- Slack Block Kit structure
- Telegram/Slack notifier isolation
- task-aware notification header
- dashboard metrics
- dashboard Job/Task mapping
- optional HTML UI disable
- all previous R0-R5 regression tests

Real Slack Socket Mode E2E still requires a Slack workspace, app tokens and an allowlisted Slack account.
