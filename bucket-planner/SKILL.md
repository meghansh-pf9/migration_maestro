---
name: vjailbreak-bucket-planner
description: >
  Generate a mock vjailbreak migration bucket plan from live VMwareMachine inventory.
  Use when the user wants to plan, organize, or create migration buckets from a VMware
  inventory. Triggers on: "create buckets", "plan migration", "bucket plan",
  "organize VMs for migration", "migration planner", "how should I group my VMs",
  "generate bucket plan". Fetches live VM data, applies the bucket creation ruleset,
  and outputs a complete plan with per-bucket config, VM assignments, and agent
  scale-up suggestion. No destination cluster is set — omit it from all bucket configs.
---

# vjailbreak Bucket Planner

Fetch live inventory, classify VMs, group them into migration buckets, and output
a complete mock plan ready for CR creation.

---

## Inputs required from the user (or form)

| Input | How to get it |
|---|---|
| Network mappings | source network → target network (one per unique source network) |
| Storage mappings | source datastore → target volume type |
| Agent flavor vCPUs | auto-detect from VjailbreakNode worker nodes |
| Max agents | user-provided |

No destination cluster is needed — omit from all bucket configs.

---

## Step 1 — Fetch inventory

```bash
kubectl get vmwaremachines -n migration-system -o json > /tmp/vms.json
kubectl get vjailbreaknodes -n migration-system -o json > /tmp/vjb.json
kubectl get configmap vjailbreak-settings -n migration-system -o json > /tmp/settings.json
```

Parse `/tmp/vms.json` to extract per VM:
- `name`, `vmid`, `migration_key` = `name-vmid.replace("vm-","")`
- `vmState` (poweredOff | running)
- `osFamily` (windowsGuest | linuxGuest)
- `networkInterfaces[]` → `nic_count`
- `disks[]` with `capacityGB` → `disk_count`, `total_disk_gb`
- `datastores[]`, `networks[]`, `clusterName`
- `Status.Migrated`

---

## Step 2 — Auto-detect agent flavor

From `/tmp/vjb.json`, find worker nodes (`spec.nodeRole == "worker"`, `status.phase == "NodeReady"`).
For each worker:
```bash
kubectl get node <worker-name> -o jsonpath='{.status.allocatable.cpu}'
```
Parse CPU: `"8"` → 8, `"8000m"` → 8, `"7580m"` → 7 (floor milliCPU / 1000).
`F` = allocatable CPU of one worker node. If no workers, use master node.

---

## Step 3 — Classify each VM

```
large = disk_count > 1  OR  total_disk_gb >= 200
small = disk_count == 1 AND total_disk_gb < 200

flagged  = name (case-insensitive) contains: dnd, do-not, do_not, dont
migrated = Status.Migrated == true  →  SKIP entirely, do not include in any bucket
```

---

## Step 4 — Assign migration type and flags

```
Migration type:
  poweredOff               → cold
  running + large          → hot  +  adminInitiatedCutOver: true
  running + small          → cold

Per-VM flags (applied to the whole bucket if ANY VM in it matches):
  Windows (windowsGuest)   → removeVMwareTools: true
  ALL VMs                  → disconnectSourceNetwork: false
  flagged VMs              → removeVMwareTools: true, disconnectSourceNetwork: false
```

---

## Step 5 — Group VMs (inventory-driven)

**Group key**: `(migration_type, disk_complexity, nic_complexity, os_type)`
- `disk_complexity`: simple (count == 1) | multi (count > 1)
- `nic_complexity`:  simple (count == 1) | multi (count > 1)

Only create a group if it has ≥ 1 VM. Non-flagged VMs → their matching group.
Flagged VMs → special `flagged-review` group (always last).

**Priority order** (simplest → most complex, determines bucket numbering):

```
 1.  cold · simple-disk · simple-NIC · linux
 2.  cold · simple-disk · simple-NIC · windows
 3.  cold · simple-disk · multi-NIC  · linux
 4.  cold · simple-disk · multi-NIC  · windows
 5.  cold · multi-disk  · simple-NIC · linux
 6.  cold · multi-disk  · simple-NIC · windows
 7.  cold · multi-disk  · multi-NIC  · linux
 8.  cold · multi-disk  · multi-NIC  · windows
 9.  cold · running-small · simple-disk · linux    ← running but small → cold
10.  cold · running-small · simple-disk · windows
11.  cold · running-small · multi-disk  · linux
12.  cold · running-small · multi-disk  · windows
13.  hot  · simple-disk · simple-NIC · linux
14.  hot  · simple-disk · simple-NIC · windows
15.  hot  · simple-disk · multi-NIC  · linux
16.  hot  · simple-disk · multi-NIC  · windows
17.  hot  · multi-disk  · simple-NIC · linux
18.  hot  · multi-disk  · simple-NIC · windows
19.  hot  · multi-disk  · multi-NIC  · linux
20.  hot  · multi-disk  · multi-NIC  · windows
21.  flagged-review                              ← always last
```

**Simplify names**: drop dimensions that are uniform across ALL buckets.
(e.g. if all VMs are linux, drop `os` from bucket names)

---

## Step 6 — Split and name buckets

- Sort VMs within each group by `total_disk_gb ASC`
- Split at **max 10 VMs per bucket** → suffix `-1`, `-2`, etc.
- Name pattern: `{type}-{disk}-{nic}-{os}-{n}`
  - Examples: `cold-simple-simple-linux-1`, `hot-multi-multi-windows-1`

---

## Step 7 — Build per-bucket config

```yaml
name:                    {generated name}
vms:                     [migration_key, ...]
migration_type:          cold | hot
disconnectSourceNetwork: false                     # all buckets
removeVMwareTools:       true | false              # true if any Windows or flagged VM
adminInitiatedCutOver:   true | false              # true only for hot buckets
networkMapping:          {from user input}
storageMapping:          {from user input}
# no destCluster
```

---

## Step 8 — Agent suggestion

```
C     = V2VHelperPodCPURequest from vjailbreak-settings ConfigMap (default 2)
t     = total VMs across all non-flagged buckets
D     = total disk count across all those VMs (sum of disk_count per VM)
F     = agent flavor vCPUs (from Step 2)
A_max = max agents (from user input)

# Fetch node CPU usage
master_name  = VjailbreakNode with nodeRole == master (or empty)
worker_names = VjailbreakNode with nodeRole == worker AND phase == NodeReady

m   = master allocatable CPU − used CPU (floor 0)
ΣΔ  = Σ max(0, allocatable_i − used_i) for each ready worker

used(node) = sum of CPU requests of pods scheduled on that node:
  kubectl get pods -n migration-system -o json
  filter by spec.nodeName == <node-name>
  sum container.resources.requests.cpu (parse milliCPU)

A_cpu  = ceil(max(0, t*C − (m + ΣΔ)) / F)
D_free = (1 + len(worker_names)) × 20     # 20 disk slots per VJB node
A_disk = ceil(max(0, D − D_free) / 20)
A      = clamp(max(A_cpu, A_disk), 0, A_max)
```

Show the formula values and state which constraint (CPU or disk) is the bottleneck.

---

## Output format

### 1. Summary table
| # | Bucket | VMs | Type | Disk | NIC | OS | removeVMwareTools | adminCutover | disconnectSrcNet |

### 2. Per-bucket detail
For each bucket, list VMs with: name · power_state · os · disk_count · total_gb · nic_count

### 3. Agent suggestion
Show all formula values. State bottleneck. Example:
> 25 VMs × 2 CPU = 50 cores needed; 14 free now (master 6 + workers 8); each agent adds 8 → **A_cpu = 5**
> 25 VMs × 2 disks avg = 50 disk slots; 40 free → **A_disk = 1**
> Bottleneck: **CPU** → recommend **5 new agents**

### 4. Flagged VMs
List all flagged VMs with their flag reason, and confirm they are in `flagged-review` (last bucket).
