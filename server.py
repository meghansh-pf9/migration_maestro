import json
import os
import subprocess
import uuid

import anthropic
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

app = FastAPI(title="vjailbreak Migration Assistant")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------------------------------------------------------------------------
# Bucket planner system prompt
# ---------------------------------------------------------------------------
BUCKET_SYSTEM_PROMPT = """You are a vjailbreak migration bucket planner.
The user's message contains pre-parsed inventory data (INVENTORY JSON) and agent info.
Apply the bucket creation ruleset to that data and output a complete mock bucket plan.

## Tools
- `run_shell` — kubectl, python3, bash. KUBECONFIG=/root/.kube/kub.yaml.
- Use run_shell ONLY for: vjailbreak-settings ConfigMap (C value) and node CPU usage (m, ΣΔ).
- Do NOT run kubectl to fetch VMwareMachines or VjailbreakNodes — data is already in the message.

## Bucket creation ruleset

### Step 1 — Read pre-parsed inventory
The message contains an INVENTORY JSON array. Each VM object has:
  name, migration_key, power_state (poweredOff|running), os_family (windowsGuest|linuxGuest),
  nic_count, disk_count, total_disk_gb, networks[], datastores[],
  cpu, memory_mb, migrated (bool), flagged (bool)

Skip VMs where migrated == true.
Flagged VMs (flagged == true) go to the special last bucket "flagged-review".

### Step 2 — Classify each VM
  large = disk_count > 1  OR  total_disk_gb >= 200
  small = disk_count == 1 AND total_disk_gb < 200

### Step 3 — Assign migration type and flags
  poweredOff                   → type: cold
  running + large              → type: hot,  adminInitiatedCutOver: true
  running + small              → type: cold

  Windows (windowsGuest)       → removeVMwareTools: true
  ALL VMs                      → disconnectSourceNetwork: false
  flagged VMs                  → removeVMwareTools: true, disconnectSourceNetwork: false

### Step 4 — Group VMs (inventory-driven, only non-empty groups)
  Group key: (migration_type, disk_complexity, nic_complexity, os_type)
    disk_complexity: simple (count==1) | multi (count>1)
    nic_complexity:  simple (count==1) | multi (count>1)

  Non-flagged VMs → assign to matching group
  Flagged VMs     → always go to the last special bucket "flagged-review"

  Priority order (lowest complexity first):
    1.  cold · simple-disk · simple-NIC · linux
    2.  cold · simple-disk · simple-NIC · windows
    3.  cold · simple-disk · multi-NIC  · linux
    4.  cold · simple-disk · multi-NIC  · windows
    5.  cold · multi-disk  · simple-NIC · linux
    6.  cold · multi-disk  · simple-NIC · windows
    7.  cold · multi-disk  · multi-NIC  · linux
    8.  cold · multi-disk  · multi-NIC  · windows
    9.  cold · running-small · simple-disk · linux     ← running but small → cold
    10. cold · running-small · simple-disk · windows
    11. cold · running-small · multi-disk  · linux
    12. cold · running-small · multi-disk  · windows
    13. hot  · simple-disk · simple-NIC · linux
    14. hot  · simple-disk · simple-NIC · windows
    15. hot  · simple-disk · multi-NIC  · linux
    16. hot  · simple-disk · multi-NIC  · windows
    17. hot  · multi-disk  · simple-NIC · linux
    18. hot  · multi-disk  · simple-NIC · windows
    19. hot  · multi-disk  · multi-NIC  · linux
    20. hot  · multi-disk  · multi-NIC  · windows
    21. flagged-review (always last)

### Step 5 — Split and name
  - Sort VMs within each group by total_disk_gb ASC
  - Split at max 10 VMs per bucket → bucket-1, bucket-2 ...
  - Name: {type}-{disk}-{nic}-{os}-{n}
    e.g. cold-simple-simple-linux-1, hot-multi-multi-windows-1
    Drop dimensions that are uniform across ALL buckets (e.g. if all linux, drop os)

### Step 6 — Apply user-supplied mappings to ALL buckets
  - networkMapping:  user-provided source→target pairs
  - storageMapping:  user-provided datastore→volume-type pairs
  - destCluster:     user-provided

### Step 7 — Agent suggestion (ALL data pre-fetched — do NOT run kubectl)
  The message contains AGENT_SIZING JSON with:
    v2v_cpu_request: C value
    master: {name, allocatable_cpu, used_cpu, free_cpu}
    workers: [{name, allocatable_cpu, used_cpu, free_cpu}, ...]
    worker_count: number of ready workers

  Use these directly:
    C     = agent_sizing.v2v_cpu_request  (default 2 if missing)
    F     = AGENT_VCPUS from message
    A_max = MAX_AGENTS from message
    t     = total non-flagged, non-migrated VMs
    D     = sum of disk_count across those VMs
    m     = agent_sizing.master.free_cpu  (already computed)
    ΣΔ    = sum of worker.free_cpu for each worker

  A_cpu  = ceil(max(0, t*C - (m+ΣΔ)) / F)
  D_free = (1 + worker_count) * 20
  A_disk = ceil(max(0, D - D_free) / 20)
  A      = clamp(max(A_cpu, A_disk), 0, A_max)

## isDefault logic
HAS_DEFAULT_BUCKET and EXISTING_BUCKETS are in the message.
- HAS_DEFAULT_BUCKET false → isDefault: true on the FIRST bucket only
- HAS_DEFAULT_BUCKET true  → isDefault: false on all new buckets
- flagged-review: always isDefault: false
- Skip any bucket name that already exists in EXISTING_BUCKETS

## Output format

1. **Summary table**
   | # | Bucket | VMs | Type | Disk | NIC | OS | isDefault |

2. **Per-bucket VM list** — name · power_state · os · disk_count · total_gb · NICs

3. **Agent suggestion** — formula values + bottleneck explanation

4. **Flagged VMs** — list with flag reason

5. **JSON plan block** — a fenced ```json block (the server uses this to build CRs):
```json
{
  "buckets": [
    {
      "name": "cold-simple-linux-1",
      "is_default": true,
      "migration_type": "cold",
      "admin_cutover": false,
      "remove_vmware_tools": false,
      "vms": ["migration_key_1", "migration_key_2"]
    }
  ],
  "agent": {"count": 3, "bottleneck": "cpu", "note": "..."}
}
```

Rules for each bucket in the JSON:
- migration_type: "cold" or "hot"
- admin_cutover: true only for hot buckets
- remove_vmware_tools: true if ANY VM in bucket is Windows or flagged
- vms: list of migration_keys (not names)

Be thorough. Show all buckets even if 1-2 VMs.
"""

# ---------------------------------------------------------------------------
# Shared
# ---------------------------------------------------------------------------
client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY", ""))
bucket_sessions: dict[str, list] = {}

TOOLS = [
    {
        "name": "run_shell",
        "description": (
            "Execute a shell command. Use for kubectl, python3, bash. "
            "KUBECONFIG is pre-set to /root/.kube/kub.yaml."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "Shell command to execute"}
            },
            "required": ["command"],
        },
    },
]


def execute_shell(command: str) -> str:
    try:
        env = {**os.environ, "KUBECONFIG": "/root/.kube/kub.yaml"}
        result = subprocess.run(
            command, shell=True, capture_output=True, text=True, timeout=120, env=env
        )
        output = (result.stdout or "") + (result.stderr or "")
        return output.strip() or "(no output)"
    except subprocess.TimeoutExpired:
        return "Error: timed out after 120s"
    except Exception as exc:
        return f"Error: {exc}"


# ---------------------------------------------------------------------------
# Generic agentic stream (reused by both chat and bucket planner)
# ---------------------------------------------------------------------------
async def agentic_stream(session_store: dict, session_id: str,
                         user_message: str, system_prompt: str):
    if session_id not in session_store:
        session_store[session_id] = []

    session_store[session_id].append({"role": "user", "content": user_message})
    print(f"[stream:{session_id[:8]}] start — message length {len(user_message)} chars", flush=True)

    try:
        iteration = 0
        while True:
            iteration += 1
            print(f"[stream:{session_id[:8]}] iteration {iteration} — calling Claude", flush=True)
            with client.messages.stream(
                model="claude-sonnet-4-6",
                max_tokens=16000,
                system=system_prompt,
                tools=TOOLS,
                messages=session_store[session_id],
            ) as stream:
                for event in stream:
                    etype = getattr(event, "type", None)
                    if etype == "content_block_delta":
                        delta = event.delta
                        if getattr(delta, "type", None) == "text_delta":
                            yield f"data: {json.dumps({'type': 'text', 'content': delta.text})}\n\n"
                    elif etype == "content_block_start":
                        block = event.content_block
                        if getattr(block, "type", None) == "tool_use":
                            yield f"data: {json.dumps({'type': 'tool_start', 'id': block.id, 'name': block.name})}\n\n"

                message = stream.get_final_message()

            print(f"[stream:{session_id[:8]}] stop_reason={message.stop_reason}", flush=True)
            session_store[session_id].append({"role": "assistant", "content": message.content})

            if message.stop_reason == "tool_use":
                tool_results = []
                for block in message.content:
                    if block.type != "tool_use":
                        continue
                    cmd = block.input.get("command", "")
                    print(f"[stream:{session_id[:8]}] tool={block.name} cmd={cmd[:80]}", flush=True)
                    yield f"data: {json.dumps({'type': 'tool_call', 'id': block.id, 'name': block.name, 'command': cmd})}\n\n"
                    result = execute_shell(cmd)
                    print(f"[stream:{session_id[:8]}] tool={block.name} result_len={len(result)}", flush=True)
                    yield f"data: {json.dumps({'type': 'tool_result', 'id': block.id, 'content': result})}\n\n"
                    tool_results.append(
                        {"type": "tool_result", "tool_use_id": block.id, "content": result}
                    )
                session_store[session_id].append({"role": "user", "content": tool_results})
            else:
                print(f"[stream:{session_id[:8]}] done", flush=True)
                break

    except Exception as exc:
        print(f"[stream:{session_id[:8]}] error: {exc}", flush=True)
        yield f"data: {json.dumps({'type': 'error', 'content': str(exc)})}\n\n"

    yield f"data: {json.dumps({'type': 'done'})}\n\n"


# ---------------------------------------------------------------------------
# Routes — bucket planner
# ---------------------------------------------------------------------------
def _parse_cpu_cores(cpu_str: str) -> int:
    """Parse k8s CPU string ('8', '8000m', '7580m') → integer cores (floor)."""
    s = cpu_str.strip().strip("'\"")
    if s.endswith("m"):
        return int(s[:-1]) // 1000
    try:
        return int(float(s))
    except ValueError:
        return 0


FLAG_WORDS = {"dnd", "do-not", "do_not", "dont"}

def _is_flagged(name: str) -> bool:
    low = name.lower()
    return any(w in low for w in FLAG_WORDS)


# Global store: migration_key → full raw VM data (for formValues construction)
raw_vm_store: dict[str, dict] = {}


def _parse_vms(vms_data: dict) -> tuple[list[dict], set, set]:
    """
    Parse VMwareMachine CRs using exact JSON field paths from the Go types.

    VMwareMachine fields used:
      spec.vms.name, .vmid, .vmState, .osFamily,
      .networkInterfaces[] (each has .network, .mac, .order, ...),
      .disks[].capacityGB, .datastores[], .networks[],
      .clusterName, .cpu, .memory
      status.powerState   (preferred over spec.vms.vmState)
      status.migrated
    """
    vms = []
    src_networks: set = set()
    src_datastores: set = set()

    for item in vms_data.get("items", []):
        spec_vms = item.get("spec", {}).get("vms", {}) or {}
        status   = item.get("status", {}) or {}

        name  = spec_vms.get("name", "") or item.get("metadata", {}).get("name", "")
        vmid  = spec_vms.get("vmid", "")

        # migration_key = "<name>-<moid>" where moid = vmid with "vm-" stripped
        moid          = vmid.replace("vm-", "") if vmid else ""
        migration_key = f"{name}-{moid}" if moid else name

        # Power state: prefer status.powerState, fall back to spec.vms.vmState
        power_state = (status.get("powerState") or spec_vms.get("vmState") or "").lower()
        # Normalise to "poweredOff" | "running"
        if power_state in ("poweredoff", "powered_off", "off"):
            power_state = "poweredOff"
        elif power_state in ("poweredon", "powered_on", "on", "running"):
            power_state = "running"

        os_family = spec_vms.get("osFamily", "")

        # NICs: spec.vms.networkInterfaces is a list of NIC objects
        nics      = spec_vms.get("networkInterfaces", []) or []
        nic_count = len(nics)

        # Disks: spec.vms.disks[].capacityGB
        disks      = spec_vms.get("disks", []) or []
        disk_count = len(disks)
        total_gb   = sum(d.get("capacityGB", 0) for d in disks if isinstance(d, dict))

        # Source networks and datastores (arrays of strings)
        networks   = [n for n in (spec_vms.get("networks", []) or []) if n]
        datastores = [d for d in (spec_vms.get("datastores", []) or []) if d]
        src_networks.update(networks)
        src_datastores.update(datastores)

        migrated     = bool(status.get("migrated", False))
        flagged      = _is_flagged(name)
        meta         = item.get("metadata", {}) or {}
        meta_labels  = meta.get("labels", {}) or {}
        cluster_name = spec_vms.get("clusterName", "")
        esx_name     = spec_vms.get("esxiName", "") or meta_labels.get("vjailbreak.k8s.pf9.io/esxi-name", "")

        vm_entry = {
            "name":          name,
            "vmid":          vmid,
            "migration_key": migration_key,
            "power_state":   power_state,
            "os_family":     os_family,
            "nic_count":     nic_count,
            "disk_count":    disk_count,
            "total_disk_gb": total_gb,
            "networks":      networks,
            "datastores":    datastores,
            "cpu":           spec_vms.get("cpu", 0),
            "memory_mb":     spec_vms.get("memory", 0),
            "cluster_name":  cluster_name,
            "migrated":      migrated,
            "flagged":       flagged,
        }
        vms.append(vm_entry)

        # Store full raw data for server-side formValues construction
        raw_vm_store[migration_key] = {
            "id":               migration_key,
            "vmKey":            migration_key,
            "name":             name,
            "vmid":             vmid,
            "vmWareMachineName": meta.get("name", name),
            "cpuCount":         spec_vms.get("cpu", 0),
            "memory":           spec_vms.get("memory", 0),
            "osFamily":         os_family,
            "powerState":       power_state,
            "vmState":          spec_vms.get("vmState", ""),
            "networks":         networks,
            "datastores":       datastores,
            "disks":            spec_vms.get("disks", []) or [],
            "networkInterfaces": nics,
            "ipAddress":        spec_vms.get("ipAddress", "") or "—",
            "isMigrated":       migrated,
            "labels":           meta_labels,
            "esxHost":          esx_name,
            "clusterName":      cluster_name,
            "flavor":           "",
            "flavorNotFound":   False,
            "hasSharedRdm":     False,
            "rdmDependencies":  [],
            "rdmDisks":         [],
            "ipValidationMessage": "",
            "ipValidationStatus":  "pending",
        }

    return vms, src_networks, src_datastores


def _parse_openstack(os_data: dict) -> dict:
    """
    Parse OpenstackCreds using exact field paths.

    OpenstackCreds fields used:
      status.openstack.networks[]        → PCDNetworkInfo {name, tags[]}
      status.openstack.volumeTypes[]     → []string
      status.openstack.volumeBackends[]  → []string
      status.openstack.securityGroups[]  → SecurityGroupInfo {name, id, ...}
      status.openstack.serverGroups[]    → ServerGroupInfo {name, id, ...}
      spec.pcdHostConfig[].clusterName   → cluster names
    """
    if not os_data.get("items"):
        return {}

    cred      = os_data["items"][0]
    os_st     = cred.get("status", {}).get("openstack", {}) or {}
    spec      = cred.get("spec", {}) or {}
    cred_name = cred.get("metadata", {}).get("name", "openstack")

    # networks: list of PCDNetworkInfo {name, tags}
    tgt_networks = []
    for n in os_st.get("networks", []) or []:
        name = (n.get("name") if isinstance(n, dict) else str(n)) or ""
        if name:
            tgt_networks.append(name)

    # volumeTypes: []string directly
    tgt_volumes = [str(v) for v in (os_st.get("volumeTypes", []) or []) if v]

    # volumeBackends: []string
    tgt_backends = [str(v) for v in (os_st.get("volumeBackends", []) or []) if v]

    # securityGroups: list of {name, id, ...}
    sec_groups = []
    for sg in (os_st.get("securityGroups", []) or []):
        name = (sg.get("name") if isinstance(sg, dict) else str(sg)) or ""
        if name:
            sec_groups.append(name)

    # serverGroups: list of {name, id, ...}
    srv_groups = []
    for sg in (os_st.get("serverGroups", []) or []):
        name = (sg.get("name") if isinstance(sg, dict) else str(sg)) or ""
        if name:
            srv_groups.append(name)

    # clusters: spec.pcdHostConfig[].clusterName and .id
    clusters = []
    pcd_cluster = ""
    for h in (spec.get("pcdHostConfig", []) or []):
        if not isinstance(h, dict):
            continue
        cluster_name = h.get("clusterName", "") or h.get("name", "") or ""
        cluster_id   = h.get("id", "") or cluster_name
        if cluster_name:
            clusters.append(cluster_name)
        if not pcd_cluster and (cluster_id or cluster_name):
            # formValues.pcdCluster expects the cluster ID (falls back to name)
            pcd_cluster = cluster_id or cluster_name

    return {
        "cred_name":        cred_name,
        "pcd_cluster":      pcd_cluster,
        "target_networks":  tgt_networks,
        "target_volumes":   tgt_volumes,
        "target_backends":  tgt_backends,
        "security_groups":  sec_groups,
        "server_groups":    srv_groups,
        "clusters":         clusters,
    }


def _fetch_agent_sizing(vjb_data: dict) -> dict:
    """
    Pre-fetch ALL data needed for the agent formula so Claude needs zero kubectl calls.
    Returns: v2v_cpu_request, master/worker allocatable+used CPU, disk slots info.
    """
    # C — V2VHelperPodCPURequest from vjailbreak-settings ConfigMap
    v2v_raw = execute_shell(
        "kubectl get configmap vjailbreak-settings -n migration-system "
        "-o jsonpath='{.data.V2VHelperPodCPURequest}' 2>/dev/null"
    )
    try:
        v2v_cpu = int(float(v2v_raw.strip().strip("'"))) if v2v_raw.strip().strip("'") else 2
    except ValueError:
        v2v_cpu = 2

    # Collect node names from VjailbreakNodes
    master_name, worker_names = None, []
    for item in vjb_data.get("items", []):
        role  = (item.get("spec") or {}).get("nodeRole", "")
        phase = (item.get("status") or {}).get("phase", "")
        name  = (item.get("metadata") or {}).get("name", "")
        if not name:
            continue
        if role == "worker" and phase == "NodeReady":
            worker_names.append(name)
        elif role in ("master", "") and master_name is None:
            master_name = name

    # Get all pods in migration-system (for used CPU calculation)
    pods_raw = execute_shell("kubectl get pods -n migration-system -o json 2>/dev/null")
    pods_data = {}
    try:
        pods_json = json.loads(pods_raw) if pods_raw and not pods_raw.startswith("Error") else {}
        for pod in pods_json.get("items", []):
            node = (pod.get("spec") or {}).get("nodeName", "")
            if not node:
                continue
            used = pods_data.get(node, 0)
            for c in (pod.get("spec") or {}).get("containers", []):
                cpu_req = ((c.get("resources") or {}).get("requests") or {}).get("cpu", "0")
                used += _parse_cpu_cores(str(cpu_req)) if not str(cpu_req).endswith("m") \
                    else int(str(cpu_req)[:-1]) / 1000
            pods_data[node] = used
    except Exception:
        pass

    def node_info(name: str) -> dict:
        alloc_raw = execute_shell(
            f"kubectl get node {name} -o jsonpath='{{.status.allocatable.cpu}}' 2>/dev/null"
        )
        alloc = _parse_cpu_cores(alloc_raw)
        used  = int(pods_data.get(name, 0))
        return {"name": name, "allocatable_cpu": alloc, "used_cpu": used, "free_cpu": max(0, alloc - used)}

    master_info  = node_info(master_name) if master_name else None
    workers_info = [node_info(n) for n in worker_names]

    return {
        "v2v_cpu_request": v2v_cpu,
        "master":          master_info,
        "workers":         workers_info,
        "worker_count":    len(workers_info),
        "worker_names":    worker_names,
        "master_name":     master_name,
    }


def _parse_vjailbreak_nodes(vjb_data: dict) -> tuple[int | None, int, list[str], str | None]:
    """
    Parse VjailbreakNode CRs.

    Fields used:
      spec.nodeRole   → "master" | "worker"
      status.phase    → "NodeReady" | ...
      metadata.name   → node name (matches k8s node name)

    Returns: (agent_vcpus, worker_count, worker_names, master_name)
    """
    agent_vcpus  = None
    worker_count = 0
    worker_names = []
    master_name  = None

    for item in vjb_data.get("items", []):
        role  = (item.get("spec", {}) or {}).get("nodeRole", "")
        phase = (item.get("status", {}) or {}).get("phase", "")
        name  = (item.get("metadata", {}) or {}).get("name", "")

        if role == "worker" and phase == "NodeReady" and name:
            worker_count += 1
            worker_names.append(name)
            if agent_vcpus is None:
                cpu_raw = execute_shell(
                    f"kubectl get node {name} -o jsonpath='{{.status.allocatable.cpu}}'"
                )
                parsed = _parse_cpu_cores(cpu_raw)
                if parsed > 0:
                    agent_vcpus = parsed

        elif role in ("master", "") and name and master_name is None:
            master_name = name

    # Fallback to master allocatable if no workers
    if agent_vcpus is None and master_name:
        cpu_raw = execute_shell(
            f"kubectl get node {master_name} -o jsonpath='{{.status.allocatable.cpu}}'"
        )
        parsed = _parse_cpu_cores(cpu_raw)
        if parsed > 0:
            agent_vcpus = parsed

    return agent_vcpus, worker_count, worker_names, master_name


@app.get("/inventory-data")
async def inventory_data():
    """
    Fetch and fully parse all inventory data server-side.
    Returns structured data ready for the form and for passing directly to Claude.
    """
    try:
        vms_raw = execute_shell("kubectl get vmwaremachines -n migration-system -o json")
        os_raw  = execute_shell("kubectl get openstackcreds  -n migration-system -o json")
        vjb_raw = execute_shell("kubectl get vjailbreaknodes -n migration-system -o json")

        def safe_parse(raw: str) -> dict:
            if raw and not raw.startswith("Error"):
                try:
                    return json.loads(raw)
                except json.JSONDecodeError:
                    pass
            return {}

        vms_data = safe_parse(vms_raw)
        os_data  = safe_parse(os_raw)
        vjb_data = safe_parse(vjb_raw)

        # Fetch VMwareCreds (need name for vmwareCredsRef)
        vc_raw  = execute_shell("kubectl get vmwarecreds -n migration-system -o json")
        vc_data = safe_parse(vc_raw)
        vmware_creds_name = ""
        for item in vc_data.get("items", []):
            phase = item.get("status", {}).get("phase", "")
            if phase == "Succeeded":
                vmware_creds_name = item.get("metadata", {}).get("name", "")
                break
        if not vmware_creds_name and vc_data.get("items"):
            vmware_creds_name = vc_data["items"][0].get("metadata", {}).get("name", "")

        # Fetch existing MigrationBuckets (to know what's already created)
        mb_raw  = execute_shell("kubectl get migrationbuckets -n migration-system -o json")
        mb_data = safe_parse(mb_raw)
        existing_buckets = []
        has_default_bucket = False
        for item in mb_data.get("items", []):
            is_def = item.get("spec", {}).get("isDefault", False)
            if is_def:
                has_default_bucket = True
            existing_buckets.append({
                "name":       item.get("metadata", {}).get("name", ""),
                "is_default": is_def,
                "vm_count":   len(item.get("spec", {}).get("vms", [])),
                "phase":      item.get("status", {}).get("phase", "NotMigrated"),
            })

        vms, src_networks, src_datastores = _parse_vms(vms_data)
        os_info    = _parse_openstack(os_data)
        agent_info = _fetch_agent_sizing(vjb_data)

        # Agent flavor vCPUs for form pre-fill
        agent_vcpus = None
        for w in agent_info.get("workers", []):
            if w.get("allocatable_cpu", 0) > 0:
                agent_vcpus = w["allocatable_cpu"]
                break
        if agent_vcpus is None and agent_info.get("master"):
            agent_vcpus = agent_info["master"].get("allocatable_cpu")

        return JSONResponse({
            # Form dropdowns
            "source_networks":   sorted(src_networks),
            "source_datastores": sorted(src_datastores),
            "target_networks":   os_info.get("target_networks", []),
            "target_volumes":    os_info.get("target_volumes", []),
            "target_backends":   os_info.get("target_backends", []),
            "security_groups":   os_info.get("security_groups", []),
            "server_groups":     os_info.get("server_groups", []),
            # Agent sizing (fully pre-fetched — passed to Claude prompt)
            "agent_vcpus":       agent_vcpus,
            "agent_sizing":      agent_info,
            # Full parsed VM list — Claude never needs kubectl for inventory
            "vms":                 vms,
            "total_vms":           len(vms),
            "skipped_migrated":    sum(1 for v in vms if v["migrated"]),
            "flagged_count":       sum(1 for v in vms if v["flagged"]),
            # CR creation context
            "vmware_creds_name":   vmware_creds_name,
            "os_creds_name":       os_info.get("cred_name", "openstack"),
            "pcd_cluster":         os_info.get("pcd_cluster", ""),
            "namespace":           "migration-system",
            "existing_buckets":    existing_buckets,
            "has_default_bucket":  has_default_bucket,
        })

    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)


@app.post("/bucket-chat")
async def bucket_chat(request: Request):
    body = await request.json()
    user_message = body.get("message", "").strip()
    session_id = body.get("session_id") or str(uuid.uuid4())
    if not user_message:
        return {"error": "empty message"}
    return StreamingResponse(
        agentic_stream(bucket_sessions, session_id, user_message, BUCKET_SYSTEM_PROMPT),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.delete("/bucket-session/{session_id}")
async def clear_bucket_session(session_id: str):
    bucket_sessions.pop(session_id, None)
    return {"status": "cleared"}


def _build_bucket_cr(bucket: dict, net_maps: list, stor_maps: list,
                      vmware_creds: str, os_creds: str, pcd_cluster: str,
                      namespace: str = "migration-system") -> dict:
    """
    Build a full MigrationBucket CR dict from a bucket plan entry + raw VM data.
    Constructs proper formValues matching the vjailbreak UI format.
    """
    migration_type = bucket.get("migration_type", "cold")
    admin_cutover  = bucket.get("admin_cutover", False)
    remove_tools   = bucket.get("remove_vmware_tools", False)
    is_default     = bucket.get("is_default", False)
    vm_keys        = bucket.get("vms", [])

    # Build full VM objects for formValues.vms from raw_vm_store
    fv_vms = []
    source_cluster = ""
    for key in vm_keys:
        raw = raw_vm_store.get(key)
        if not raw:
            continue
        if not source_cluster and raw.get("clusterName"):
            source_cluster = raw["clusterName"]
        fv_vms.append(raw)

    # formValues is the verbatim Migration Form FormValues object.
    # - dataCopyMethod: "cold"|"hot" → MigrationPlan.spec.migrationStrategy.type
    # - storageCopyMethod: "normal"|"HotAdd"|"StorageAcceleratedCopy" → MigrationTemplate.spec.storageCopyMethod
    # - disconnectSourceNetwork / networkPersistence / removeVMwareTools live HERE (not in selectedOptions)
    # - cutoverOption: "" by default; only populated when selectedOptions.cutoverOption = true
    form_values = {
        "vmwareCreds":            {"existingCredName": vmware_creds},
        "openstackCreds":         {"existingCredName": os_creds},
        "vmwareCluster":          source_cluster,   # "credName:datacenter:cluster" from VMInfo.clusterName
        "pcdCluster":             pcd_cluster,       # PCD cluster ID from pcdHostConfig[0].clusterName
        "vms":                    fv_vms,             # full VmData objects
        "networkMappings":        net_maps,
        "storageMappings":        stor_maps,
        "dataCopyMethod":         migration_type,    # "cold" | "hot"
        "storageCopyMethod":      "normal",           # disk pipeline; "normal" always for now
        "disconnectSourceNetwork": False,             # always false = keep source NIC connected
        "fallbackToDHCP":          False,
        "networkPersistence":      True,              # always true
        "removeVMwareTools":       remove_tools,
        "securityGroups":          [],
        "serverGroup":             "",
        "imageProfiles":           [],
        "dataCopyStartTime":       "",
        "cutoverOption":           "",               # empty; launcher ignores unless selectedOptions.cutoverOption=true
        "cutoverStartTime":        "",
        "cutoverEndTime":          "",
        "proxyVMRef":              "",
        "arrayCredsMappings":      [],
    }

    # selectedOptions: only the "which optional fields are enabled" toggles — all false
    selected_options = {
        "dataCopyMethod":    False,
        "dataCopyStartTime": False,
        "cutoverOption":     False,
        "cutoverStartTime":  False,
        "cutoverEndTime":    False,
        "postMigrationScript": False,
        "useGPU":            False,
        "useFlavorless":     False,
        "postMigrationAction": {
            "suffix": False, "folderName": False,
            "renameVm": False, "moveToFolder": False,
        },
    }

    return {
        "apiVersion": "vjailbreak.k8s.pf9.io/v1alpha1",
        "kind":       "MigrationBucket",
        "metadata":   {"name": bucket["name"], "namespace": namespace},
        "spec": {
            "vmwareCredsRef": {"name": vmware_creds},
            "vms":             vm_keys,
            "isDefault":       is_default,
            "config": {
                "dataCopyMethod":   migration_type,
                "networkMappings":  net_maps,
                "storageMappings":  stor_maps,
                "sourceCluster":    source_cluster,
                "formValues":       form_values,
                "selectedOptions":  selected_options,
            },
        },
    }


@app.post("/apply-buckets")
async def apply_buckets(request: Request):
    """
    Accept a JSON bucket plan from Claude, build full MigrationBucket CRs
    with proper formValues server-side, and apply them.
    """
    import tempfile
    import yaml as _yaml

    body = await request.json()
    plan      = body.get("plan", {})       # {"buckets": [...], "agent": {...}}
    net_maps  = body.get("net_maps", [])   # [{source, target}]
    stor_maps = body.get("stor_maps", [])  # [{source, target}]

    buckets = plan.get("buckets", [])
    if not buckets:
        return JSONResponse({"error": "no buckets in plan"}, status_code=400)

    # Fetch context needed for CR construction
    vmware_creds = body.get("vmware_creds", "vmware")
    os_creds     = body.get("os_creds",     "openstack")
    pcd_cluster  = body.get("pcd_cluster",  "")
    namespace    = "migration-system"

    yaml_docs = []
    for b in buckets:
        cr = _build_bucket_cr(b, net_maps, stor_maps,
                              vmware_creds, os_creds, pcd_cluster, namespace)
        yaml_docs.append(_yaml.dump(cr, default_flow_style=False))

    combined_yaml = "---\n" + "\n---\n".join(yaml_docs)

    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
        f.write(combined_yaml)
        tmp_path = f.name

    result = execute_shell(f"kubectl apply -f {tmp_path} -n {namespace}")
    os.unlink(tmp_path)

    print(f"[apply-buckets] applied {len(buckets)} buckets: {result[:200]}", flush=True)
    return JSONResponse({"output": result, "bucket_count": len(buckets)})


# ---------------------------------------------------------------------------
# Health + static
# ---------------------------------------------------------------------------
@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/kubeconfig-status")
async def kubeconfig_status():
    """Check if a kubeconfig is present and the cluster is reachable."""
    kube_path = os.environ.get("KUBECONFIG", "/root/.kube/kub.yaml")
    if not os.path.exists(kube_path):
        return JSONResponse({"connected": False, "message": "No kubeconfig found"})
    result = execute_shell("kubectl cluster-info --request-timeout=4s 2>&1 | head -2")
    connected = "Kubernetes control plane" in result or "running at" in result
    server = ""
    try:
        import yaml as _yaml
        with open(kube_path) as f:
            kc = _yaml.safe_load(f)
        clusters = kc.get("clusters", [])
        if clusters:
            server = clusters[0].get("cluster", {}).get("server", "")
    except Exception:
        pass
    return JSONResponse({
        "connected": connected,
        "server":    server,
        "message":   result.split("\n")[0] if connected else result[:120],
    })


@app.post("/upload-kubeconfig")
async def upload_kubeconfig(request: Request):
    """Accept kubeconfig content (raw YAML text) and save it."""
    body = await request.json()
    content = body.get("content", "").strip()
    if not content:
        return JSONResponse({"error": "empty content"}, status_code=400)

    # Basic sanity check
    if "clusters:" not in content and "server:" not in content:
        return JSONResponse({"error": "does not look like a valid kubeconfig"}, status_code=400)

    kube_path = os.environ.get("KUBECONFIG", "/root/.kube/kub.yaml")
    os.makedirs(os.path.dirname(kube_path), exist_ok=True)
    with open(kube_path, "w") as f:
        f.write(content)

    # Test connection
    result = execute_shell("kubectl cluster-info --request-timeout=4s 2>&1 | head -2")
    connected = "Kubernetes control plane" in result or "running at" in result
    return JSONResponse({
        "saved":     True,
        "connected": connected,
        "message":   result.split("\n")[0] if connected else result[:120],
    })


app.mount("/static", StaticFiles(directory="/app/static"), name="static")


@app.get("/")
async def root():
    return FileResponse("/app/static/index.html")
