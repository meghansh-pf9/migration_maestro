---
name: vjailbreak-migrate
description: >
  Trigger a VMware→OpenStack/PCD VM migration on a Platform9 vjailbreak appliance
  (k3s cluster with the vjailbreak.k8s.pf9.io operator). Use this skill whenever
  the user wants to list source VMs, pick migration options/mappings, and start a
  migration via kubectl. Also use when the user mentions "migrate a VM", "vjailbreak",
  "vmware to openstack", "vmware to pcd", "migration-system", "virt-v2v migration",
  "cold migration", "hot migration", "MigrationPlan", "MigrationTemplate",
  "NetworkMapping", or "StorageMapping". Walks through: discovery → ask VM + type +
  network/storage mappings → build & apply CRs → monitor progress.
---

# vjailbreak Migration Workflow

This skill drives [Platform9 vjailbreak](https://github.com/platform9/vjailbreak) — a k3s-based appliance whose operator migrates VMs from VMware vSphere to OpenStack / Platform9 Private Cloud Director (PCD).

A migration requires applying four CRs (`vjailbreak.k8s.pf9.io/v1alpha1`):
`NetworkMapping`, `StorageMapping`, `MigrationTemplate`, `MigrationPlan`.

The operator auto-creates a `Migration` CR + a `v2v-helper` Job/Pod that copies disks (VDDK/NBD → virt-v2v → Cinder volume) and boots the VM on the target.

All resources live in namespace **`migration-system`** (override with `$NS` if different).

---

## Safety rules (read first)

- **Confirm authorization before applying anything.** A `cold`/`hot` migration moves real disk data against real vCenter + cloud endpoints; cutover can boot a clone and (if enabled) disconnect the source NIC. The source VM itself is preserved by default (`disconnectSourceNetwork: false`).
- **Prefer `mock` for any test/dry run** — it exercises the full controller→pod path without copying real bytes.
- **Watch for "do-not-delete" / "dont-migrate" / "dnd" in VM names** — surface them and require explicit confirmation before proceeding.
- Always run `kubectl apply --dry-run=server` before the real apply.
- `cold` requires the source VM **powered off**; `hot`/warm works on running VMs.

---

## Step 1 — Verify the environment

Confirm kubectl works, it's a vjailbreak cluster, and both credential CRs validated:

```bash
NS=${NS:-migration-system}
kubectl version 2>/dev/null | grep -i server || { echo "no cluster"; exit 1; }
echo "== vjailbreak CRDs present =="
kubectl get crd 2>/dev/null | grep -c 'vjailbreak.k8s.pf9.io' | xargs echo "vjailbreak CRD count:"
echo "== credentials (must be Succeeded) =="
kubectl get vmwarecreds,openstackcreds -n "$NS" 2>&1
```

Both `vmwarecreds` and `openstackcreds` must show `Succeeded`. If not, stop and tell the user to fix credentials in the UI first.

---

## Step 2 — List all source VMs

VMs are discovered as `vmwaremachines` CRs. The **migration key** for a VM is `"<displayName>-<moid>"` (the `vmid` with the `vm-` prefix stripped) — this exact string goes into the MigrationPlan.

```bash
NS=${NS:-migration-system}
kubectl get vmwaremachines -n "$NS" -o json > /tmp/vjb_vms.json
python3 - <<'PY'
import json
d=json.load(open("/tmp/vjb_vms.json"))
rows=[]; nets=set(); ds=set()
for it in d["items"]:
    s=it["spec"].get("vms",{}) or {}
    name=s.get("name","?"); vmid=s.get("vmid","")
    key=name+"-"+vmid.replace("vm-","") if vmid else name
    disks=s.get("disks",[]) or []
    cap=sum(x.get("capacityGB",0) for x in disks)
    nlist=sorted(set(s.get("networks",[]) or []))
    dlist=sorted(set(s.get("datastores",[]) or []))
    nets.update(nlist); ds.update(dlist)
    rows.append((name,key,s.get("vmState","?"),s.get("osFamily","?"),
                 s.get("cpu","?"),s.get("memory","?"),len(disks),cap,
                 ",".join(nlist),",".join(dlist)))
rows.sort(key=lambda r:(r[2]!="running", r[0].lower()))
print("%-40s %-44s %-11s %-12s %3s %6s %2s %5s  %s" %
      ("NAME","MIGRATION_KEY","STATE","OSFAMILY","CPU","RAM","D","GB","NET / DS"))
print("-"*170)
for r in rows:
    print("%-40.40s %-44.44s %-11.11s %-12.12s %3s %6s %2s %5s  %s | %s" %
          (r[0],r[1],r[2],r[3],r[4],r[5],r[6],r[7],r[8],r[9]))
print("\nTOTAL %d VMs (running=%d)" % (len(rows), sum(1 for r in rows if r[2]=="running")))
print("SOURCE NETWORKS :", sorted(nets))
print("SOURCE DATASTORES:", sorted(ds))
risky=[r[0] for r in rows if any(k in r[0].lower() for k in ("do-not","dont","dnd","do_not"))]
if risky: print("\n⚠ DO-NOT-TOUCH candidates:", risky)
PY
```

---

## Step 3 — Gather target (OpenStack/PCD) options

```bash
NS=${NS:-migration-system}
kubectl get openstackcreds -n "$NS" -o json > /tmp/vjb_os.json
python3 - <<'PY'
import json
d=json.load(open("/tmp/vjb_os.json"))
os=d["items"][0]["status"]["openstack"]
def names(v): return [x.get("name") or x.get("Name") for x in v] if v and isinstance(v[0],dict) else v
print("TARGET NETWORKS   :", names(os.get("networks",[])))
print("VOLUME TYPES      :", names(os.get("volumeTypes",[])))
print("SECURITY GROUPS   :", names(os.get("securityGroups",[])))
print("VOLUME BACKENDS   :", os.get("volumeBackends",[]))
PY
```

---

## Step 4 — Ask the user

Use the AskUserQuestion tool to confirm these, pre-filling options from Steps 2–3:

1. **VM** — which VM to migrate (MIGRATION_KEY from Step 2). Recommend a small/safe powered-off VM. For `cold`, VM must be powered off.
2. **Migration type** — `mock` (recommended/safe), `cold` (real, VM off), or `hot` (real, VM running).
3. **Network mapping** — each source network → target network (from Step 3).
4. **Storage mapping** — each source datastore → target volume type (from Step 3).

Optional (use sensible defaults): security group (default `default`), `adminInitiatedCutOver` (default `false`), `disconnectSourceNetwork` (default `false`).

---

## Step 5 — Generate the manifests

```bash
NS=${NS:-migration-system}
# ---- fill from user's answers ----
PLAN=demo-cold-migration
VM_KEY="demo-vm-7424"           # MIGRATION_KEY from Step 2
OS_FAMILY=linuxGuest            # linuxGuest | windowsGuest
MIG_TYPE=cold                   # mock | cold | hot
SRC_NET="VM Network"; DST_NET="Physnet1"
SRC_DS="datastore-nfs"; DST_VOLTYPE="nfs-hotadd"
ADMIN_CUTOVER=false
DISCONNECT_SRC=false
NETMAP="${PLAN}-netmap"; STOMAP="${PLAN}-stomap"; TMPL="${PLAN}-template"

cat > /tmp/${PLAN}.yaml <<EOF
apiVersion: vjailbreak.k8s.pf9.io/v1alpha1
kind: NetworkMapping
metadata: { name: ${NETMAP}, namespace: ${NS} }
spec:
  networks:
    - source: "${SRC_NET}"
      target: "${DST_NET}"
---
apiVersion: vjailbreak.k8s.pf9.io/v1alpha1
kind: StorageMapping
metadata: { name: ${STOMAP}, namespace: ${NS} }
spec:
  storages:
    - source: "${SRC_DS}"
      target: "${DST_VOLTYPE}"
---
apiVersion: vjailbreak.k8s.pf9.io/v1alpha1
kind: MigrationTemplate
metadata: { name: ${TMPL}, namespace: ${NS} }
spec:
  osFamily: ${OS_FAMILY}
  storageCopyMethod: normal
  networkMapping: ${NETMAP}
  storageMapping: ${STOMAP}
  source: { vmwareRef: vmware }
  destination: { openstackRef: openstack }
---
apiVersion: vjailbreak.k8s.pf9.io/v1alpha1
kind: MigrationPlan
metadata: { name: ${PLAN}, namespace: ${NS} }
spec:
  migrationTemplate: ${TMPL}
  migrationStrategy:
    type: ${MIG_TYPE}
    adminInitiatedCutOver: ${ADMIN_CUTOVER}
    disconnectSourceNetwork: ${DISCONNECT_SRC}
  virtualMachines:
    - - "${VM_KEY}"
EOF
echo "wrote /tmp/${PLAN}.yaml"; cat /tmp/${PLAN}.yaml
```

**Notes:**
- `vmwareRef: vmware` / `openstackRef: openstack` are the conventional cred CR names — confirm against Step 1 output and adjust if different.
- `virtualMachines` is `[[...]]`: outer list = sequential batches, inner list = parallel VMs. One VM = `- - "<key>"`.
- Multiple source networks/datastores → add more `- source:/target:` entries.
- For Windows, virt-v2v injects virtio-win drivers automatically.

---

## Step 6 — Validate, then apply

```bash
NS=${NS:-migration-system}; PLAN=${PLAN:?set PLAN}
kubectl apply -f /tmp/${PLAN}.yaml --dry-run=server   # must succeed with no errors
kubectl apply -f /tmp/${PLAN}.yaml                    # real apply — starts the migration
```

---

## Step 7 — Monitor

```bash
NS=${NS:-migration-system}; PLAN=${PLAN:?set PLAN}
echo "== plan status =="; kubectl get migrationplan ${PLAN} -n "$NS" -o jsonpath='{.status}{"\n"}'
echo "== migration(s) =="; kubectl get migrations -n "$NS" -l migrationplan=${PLAN}
echo "== helper pod =="; kubectl get pods -n "$NS" | grep v2v-helper

# Poll phase until terminal:
MIG=$(kubectl get migrations -n "$NS" -l migrationplan=${PLAN} -o jsonpath='{.items[0].metadata.name}')
for i in $(seq 1 40); do
  ph=$(kubectl get migration "$MIG" -n "$NS" -o jsonpath='{.status.phase}' 2>/dev/null)
  echo "[$(date +%H:%M:%S)] phase=$ph"
  case "$ph" in Succeeded|Failed|ValidationFailed) break;; esac
  sleep 20
done
kubectl get events -n "$NS" --sort-by=.lastTimestamp | grep -i "$MIG" | tail -20
POD=$(kubectl get pods -n "$NS" -o name | grep v2v-helper | grep "${MIG#migration-}" | head -1)
[ -n "$POD" ] && kubectl logs -n "$NS" "$POD" --tail=40 | grep -ivE '^[A-Z_]+='
```

**Phase order (cold):** `Pending → Validating → AwaitingDataCopyStart → CopyingBlocks → ConvertingDisk → Succeeded`

**Warm/hot adds:** `CopyingChangedBlocks` (CBT) then `AwaitingAdminCutOver` — waits there until you trigger cutover.

---

## Step 8 — Trigger cutover (hot/warm, or cold with adminInitiatedCutOver: true)

When phase is `AwaitingAdminCutOver`:

```bash
NS=${NS:-migration-system}
kubectl patch migration <migration-name> -n "$NS" --type merge -p '{"spec":{"initiateCutover":true}}'
```

---

## Cleanup / retry

```bash
NS=${NS:-migration-system}; PLAN=${PLAN:?set PLAN}
# Retry a failed VM: delete its Migration object, the plan controller recreates it.
kubectl delete migration -n "$NS" -l migrationplan=${PLAN}
# Tear down everything this run created:
kubectl delete -f /tmp/${PLAN}.yaml
```

---

## CR Reference (vjailbreak.k8s.pf9.io/v1alpha1)

- **Connection:** `vmwarecreds`, `openstackcreds` (must be `Succeeded`), `esxisshcreds`, `arraycreds`/`arraycredsmapping`
- **Source inventory (read-only):** `vmwareclusters`, `vmwarehosts`, `vmwaremachines`
- **Target:** `pcdclusters`, `pcdhosts`
- **Mappings:** `networkmappings`, `storagemappings`, `volumeimageprofiles`
- **Engine:** `migrationtemplates` → `migrationplans` → `migrations` (one per VM) → `v2v-helper` pod
- **Scale-out:** `rollingmigrationplans` → `clustermigrations` → `esximigrations`
- **Advanced:** `proxyvms` (hot-add), `rdmdisks` (raw device maps), `vjailbreaknodes`

**Key field facts:**
- MigrationPlan `virtualMachines` entries must equal `"<displayName>-<moid>"` (`vm-` stripped from vmid).
- `migrationStrategy.type` enum: `hot | cold | mock`
- `MigrationTemplate.storageCopyMethod` enum: `normal | StorageAcceleratedCopy | HotAdd`
  - HotAdd needs a `proxyVMRef`; StorageAcceleratedCopy needs `arrayCredsMapping`
- `networkMapping` may be empty only if selected VMs have no attached networks.
