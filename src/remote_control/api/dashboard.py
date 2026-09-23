from __future__ import annotations

from datetime import datetime, timezone

from remote_control.controller.service import ControllerService
from remote_control.controller.states import TERMINAL_STATES
from remote_control.hosts.models import HostStatus


class DashboardService:
    def __init__(self, controller: ControllerService) -> None:
        self.controller = controller

    async def snapshot(self) -> dict:
        projects = self.controller.projects.list()
        hosts = await self.controller.hosts.list() if self.controller.hosts is not None else []
        jobs = await self.controller.jobs.list(limit=1000)
        project_work = (
            await self.controller.jobs.project_work.list(limit=1000)
            if self.controller.jobs.project_work is not None
            else []
        )
        approvals = (
            await self.controller.jobs.approvals.list(limit=1000)
            if self.controller.jobs.approvals is not None
            else []
        )
        recovery = (
            await self.controller.jobs.recovery.list()
            if self.controller.jobs.recovery is not None
            else []
        )

        terminal = {state.value for state in TERMINAL_STATES}
        active_jobs = [job for job in jobs if job.state not in terminal]
        durations = [
            max((_aware(job.updated_at) - _aware(job.created_at)).total_seconds(), 0)
            for job in jobs
            if job.state in terminal
        ]
        work_by_job = {record.job_id: record for record in project_work}

        return {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "metrics": {
                "projects": len(projects),
                "active_hosts": sum(host.status == HostStatus.ONLINE for host in hosts),
                "active_jobs": len(active_jobs),
                "queued_jobs": sum(job.state == "QUEUED" for job in jobs),
                "failed_jobs": sum(job.state == "FAILED" for job in jobs),
                "average_job_duration_seconds": (
                    round(sum(durations) / len(durations), 2) if durations else 0
                ),
                "quota_wait_count": sum(job.state == "WAITING_QUOTA" for job in jobs),
                "human_gate_count": sum(
                    approval.status == "PENDING" for approval in approvals
                ),
                "recovery_count": len(recovery),
            },
            "projects": [
                {
                    "id": project.id,
                    "name": project.name,
                    "adapter": project.adapter,
                    "default_host": project.default_host,
                }
                for project in projects
            ],
            "hosts": [
                {
                    "id": host.id,
                    "name": host.name,
                    "os": host.os,
                    "status": host.status.value,
                    "capabilities": sorted(host.capabilities),
                    "last_heartbeat": (
                        host.last_heartbeat.isoformat() if host.last_heartbeat else None
                    ),
                }
                for host in hosts
            ],
            "jobs": [
                {
                    "id": job.id,
                    "project_id": job.project_id,
                    "state": job.state,
                    "host": job.assigned_host,
                    "task_id": (
                        work_by_job[job.id].task_id if job.id in work_by_job else None
                    ),
                    "adapter": (
                        work_by_job[job.id].adapter if job.id in work_by_job else None
                    ),
                    "created_at": job.created_at.isoformat(),
                    "updated_at": job.updated_at.isoformat(),
                }
                for job in jobs[:50]
            ],
            "approvals": [
                {
                    "id": approval.id,
                    "job_id": approval.job_id,
                    "type": approval.approval_type,
                    "status": approval.status,
                    "question": approval.question,
                }
                for approval in approvals
                if approval.status == "PENDING"
            ],
        }


def render_dashboard_html() -> str:
    return """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Remote Agent Control</title>
<style>
:root{color-scheme:light dark;font-family:Inter,system-ui,sans-serif}
body{max-width:1200px;margin:0 auto;padding:24px}
h1{margin-bottom:4px}.muted{opacity:.65}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px}
.card{border:1px solid color-mix(in srgb,currentColor 22%,transparent);border-radius:12px;padding:14px}
.metric{font-size:1.6rem;font-weight:700}.section{margin-top:26px}
table{width:100%;border-collapse:collapse;font-size:.92rem}th,td{text-align:left;padding:8px;border-bottom:1px solid color-mix(in srgb,currentColor 15%,transparent)}
.badge{display:inline-block;padding:2px 7px;border-radius:999px;border:1px solid currentColor;font-size:.78rem}
</style>
</head>
<body>
<h1>Remote Agent Control</h1>
<div class="muted">Read-only runtime dashboard · refreshes every 5 seconds</div>
<div id="metrics" class="grid section"></div>
<div class="section"><h2>Hosts</h2><div id="hosts"></div></div>
<div class="section"><h2>Recent Jobs</h2><div id="jobs"></div></div>
<div class="section"><h2>Pending Human Gates</h2><div id="approvals"></div></div>
<script>
const esc=v=>String(v??"").replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;","\"":"&quot;","'":"&#39;"}[c]));
const table=(cols,rows)=>'<table><thead><tr>'+cols.map(c=>'<th>'+esc(c[0])+'</th>').join('')+'</tr></thead><tbody>'+rows.map(r=>'<tr>'+cols.map(c=>'<td>'+esc(r[c[1]])+'</td>').join('')+'</tr>').join('')+'</tbody></table>';
async function refresh(){
  const r=await fetch('/dashboard',{cache:'no-store'}); if(!r.ok)return;
  const d=await r.json();
  document.getElementById('metrics').innerHTML=Object.entries(d.metrics).map(([k,v])=>'<div class="card"><div class="muted">'+esc(k)+'</div><div class="metric">'+esc(v)+'</div></div>').join('');
  document.getElementById('hosts').innerHTML=table([['Host','id'],['Status','status'],['OS','os'],['Capabilities','caps']],d.hosts.map(x=>({...x,caps:x.capabilities.join(', ')})));
  document.getElementById('jobs').innerHTML=table([['Job','id'],['Project','project_id'],['Task','task_id'],['State','state'],['Host','host']],d.jobs);
  document.getElementById('approvals').innerHTML=d.approvals.length?table([['Approval','id'],['Job','job_id'],['Type','type'],['Question','question']],d.approvals):'<div class="muted">No pending Human Gates.</div>';
}
refresh(); setInterval(refresh,5000);
</script>
</body>
</html>"""


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)
