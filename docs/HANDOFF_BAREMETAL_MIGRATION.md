# Corsair — Bare-metal home server migration handoff

**Author:** Claude (Opus 4.7) for Brian Pemble (bpemble@me.com)
**Created:** 2026-05-02
**Source host:** VPS, Linux 5.15.0-176-generic, x86_64
**Target host:** dedicated home server, TBD
**Goal:** reduce internal latency tail jitter from ~500-2000 µs (hypervisor) to ~50-100 µs (bare metal), and create a tunable platform for further latency work (RT kernel, isolcpus, NIC tuning, eventually colocation).

---

## Part 1 — What to migrate (everything that is NOT in git)

The repo at `https://github.com/bpemble/corsair.git` is the source of truth for code and configs. Everything else listed below is runtime state that lives only on the source host.

### 1.1 Critical (will not boot without these)

```
~/corsair/.env                                   IBKR credentials + account ID
                                                 SCP this. Do NOT commit it.
                                                 Contents: TWS_USERID, TWS_PASSWORD,
                                                 TRADING_MODE=paper, IBKR_ACCOUNT
```

### 1.2 Strongly recommended (history / continuity)

```
~/corsair/logs-paper/                  ~1.7 GB   v1.4 §9.5 JSONL streams. Stage 1
                                                 acceptance evaluation reads these.
                                                 Includes:
                                                   fills-YYYY-MM-DD.jsonl
                                                   kill_switch-YYYY-MM-DD.jsonl
                                                   hedge_trades-YYYY-MM-DD.jsonl
                                                   trader_events-*.jsonl
                                                   trader_decisions-*.jsonl
                                                   burst_events-*.jsonl
                                                   daily-summary-*.json
                                                   archive/  (old fills)
                                                 If you don't bring these you lose
                                                 your strategy P&L history. Sync
                                                 with rsync -av --partial.

~/corsair/logs/                                  Operational logs (broker stdout,
                                                 trader stdout). Useful for
                                                 postmortem context. Optional.

~/corsair/data/daily_state.json        small    Persisted daily P&L halt state
                                                 + session anchor. If you
                                                 cut over mid-session, brings
                                                 the new host up at the same
                                                 P&L baseline. Otherwise it
                                                 regenerates next CME open.
```

### 1.3 Optional / regenerable (skip unless you have a reason)

```
~/corsair/data/corsair_ipc.commands              SHM ring buffers — regenerated
~/corsair/data/corsair_ipc.events                on broker boot. DO NOT migrate.
~/corsair/data/corsair_ipc.commands.notify       FIFOs — recreated on boot. SKIP.
~/corsair/data/corsair_ipc.events.notify
~/corsair/data/hg_chain_snapshot.json            Live snapshot — overwritten 4 Hz.
~/corsair/data/snapshot_rust.json                Same. SKIP.

corsair_ib-gateway-data (docker volume)          Empty in our setup (gateway
                                                 stores state in container fs,
                                                 not the volume — verified
                                                 2026-05-02). SKIP.

docker images (corsair-corsair, corsair-dashboard,
               corsair-ib-gateway)               Built locally from Dockerfile +
                                                 source. Will rebuild on the new
                                                 host with `docker compose build`.
                                                 SKIP — do not export+import.

rust/target/                                     Rust build artifacts. Will
                                                 rebuild. SKIP.

~/corsair/span_data/                             Empty. Reserved for future SPAN
                                                 calibration files. SKIP.

~/corsair/.pytest_cache/                         pytest cache. SKIP.
```

### 1.4 Outside the repo

```
~/.claude/                                       (Claude Code state — only relevant
                                                 if you want to keep this assistant's
                                                 memory across hosts. The memory at
                                                 ~/.claude/projects/-home-ethereal-
                                                 corsair/memory/ has user
                                                 preferences. Optional.)

~/.ssh/                                          SSH keys + known_hosts. Standard
                                                 user-account migration. Required
                                                 for git push from the new host.

~/.gitconfig                                     git author config. Required for
                                                 commits.

~/.docker/config.json                            Docker registry creds. Optional —
                                                 only needed if you've logged into
                                                 a private registry.
```

### 1.5 Migration command sketch

```bash
# On NEW host (after fresh OS + docker install):
git clone https://github.com/bpemble/corsair.git ~/corsair
cd ~/corsair

# From OLD host, copy the runtime state:
rsync -avP \
    ~/corsair/.env \
    ~/corsair/logs-paper/ \
    ~/corsair/logs/ \
    ~/corsair/data/daily_state.json \
    new-host:~/corsair/

# On NEW host:
docker compose build
docker compose up -d
```

That's it. Everything else regenerates.

---

## Part 2 — Latency optimization plan

This section captures specific, concrete tuning steps. Each is annotated with
expected gain (best estimate from public benchmarks of similar workloads), effort,
and rollback approach. Apply in order; measure after each step.

### Phase 0 — Hardware selection (if not yet purchased)

```
Recommended baseline:
  CPU         AMD Ryzen 7 5950X (16 core, high single-thread, low jitter)
              OR Intel Xeon E-2378G (8 core, ECC, server-class)
              Avoid: heterogeneous (P+E) Intel CPUs (12th gen+) — scheduler
              jitter is harder to reason about with mixed core types.

  Memory      32-64 GB ECC DDR4-3200, dual-channel.
              ECC matters less for paper P&L than for production trading,
              but it eliminates a class of bugs that look like SVI fitter
              numerical drift.

  Storage     1 TB NVMe (Samsung 980 Pro / WD SN850X / Crucial T700).
              JSONL writes are ~50 MB/day at peak. Any modern NVMe is
              massive overkill, but spend the $30 extra for low-latency
              random I/O.

  NIC         Intel I225/I226 (built into modern boards) — fine to start.
              Future upgrade path: Solarflare X2522 / Mellanox ConnectX-5
              when you're ready for kernel-bypass.

  Network     Wired gigabit to your home router. Your ISP RTT to IBKR
              gateway dominates everything internal. Wifi is a non-starter.

  Power       UPS (CyberPower CP1500PFCLCD or similar, ~$200) is mandatory
              if this is going to run paper P&L overnight. Power blip =
              container restart = state loss.

  Cooling     Stock CPU cooler is fine for 24/7 paper. If you tune
              `governor=performance` and disable C-states, expect 30-50 W
              continuous idle draw — plan for it.
```

### Phase 1 — OS install + baseline setup

```
Distribution      Ubuntu Server 24.04 LTS or Debian 12
                  (matches the VPS, predictable kernel cadence)

Kernel            ship with whatever the distro provides (6.8.x), tune later.
                  Do NOT install -lowlatency or RT kernels yet — measure the
                  baseline first, then upgrade if numbers justify it.

Filesystem        ext4 on a single NVMe partition. ZFS / btrfs are great,
                  but they introduce I/O latency variance you don't want
                  while you're trying to characterize hot-path jitter.

Swap              swapoff -a; remove from /etc/fstab.
                  Production trading should never page out. If you OOM,
                  you want to know — not have a paging tail latency spike.

Time sync         chrony (preferred) or systemd-timesyncd. NTP drift can
                  desync your snapshot timestamps from broker fill timestamps,
                  making post-trade analysis confusing.
                    sudo apt install chrony
                    sudo systemctl enable --now chrony

Packages          docker.io docker-compose-v2 git build-essential
                  htop iotop bpftrace linux-tools-common cpuset numactl
                  ethtool tuned irqbalance util-linux msr-tools
```

### Phase 2 — Free wins (apply first, no kernel changes)

These are zero-risk, ~5 minutes to apply, ~50-200 µs of p99 jitter improvement.

```bash
# CPU governor: performance (no DVFS frequency stepping)
sudo cpupower frequency-set -g performance
echo performance | sudo tee /sys/devices/system/cpu/cpu*/cpufreq/scaling_governor

# Persist via systemd:
sudo tee /etc/systemd/system/cpu-perf.service <<'EOF'
[Unit]
Description=CPU performance governor
After=multi-user.target

[Service]
Type=oneshot
ExecStart=/usr/bin/cpupower frequency-set -g performance
RemainAfterExit=yes

[Install]
WantedBy=multi-user.target
EOF
sudo systemctl enable --now cpu-perf

# Disable irqbalance (we'll pin IRQs manually in Phase 4)
sudo systemctl disable --now irqbalance

# Disable transparent hugepages (causes unpredictable allocation latency)
echo never | sudo tee /sys/kernel/mm/transparent_hugepage/enabled
echo never | sudo tee /sys/kernel/mm/transparent_hugepage/defrag

# Persist via /etc/default/grub:
#   GRUB_CMDLINE_LINUX="transparent_hugepage=never"
# then `sudo update-grub && sudo reboot`

# Disable kernel core dumps (kernel crashes during JSONL writes were a
# rare jitter source on the VPS — they're gone, but defense in depth)
sudo sysctl -w kernel.core_pattern=/dev/null
```

**Expected gain after Phase 2:** internal TTT p99 from ~5.6 ms to ~2-3 ms.
Mean roughly unchanged. The win is jitter reduction.

### Phase 3 — Kernel cmdline (requires reboot)

This is where bare metal starts to shine. Edit `/etc/default/grub`:

```
GRUB_CMDLINE_LINUX="transparent_hugepage=never \
    intel_idle.max_cstate=0 \
    processor.max_cstate=1 \
    idle=poll \
    nosoftlockup \
    nohz_full=2-7 \
    rcu_nocbs=2-7 \
    isolcpus=2-7 \
    mitigations=off"
```

Annotation:
```
intel_idle.max_cstate=0    Disable deep C-states (wake latency: 100µs → 1µs)
processor.max_cstate=1     Same on AMD via the generic driver
idle=poll                  Don't HLT idle cores — they busy-spin instead.
                           Burns wattage but eliminates wake jitter.
nosoftlockup              Disable softlockup detector (it interferes with
                           busy loops in the trader hot path)
nohz_full=2-7              Tickless mode on cores 2-7 (no scheduler tick
                           interruption when only one task is running)
rcu_nocbs=2-7              Move RCU callback processing off cores 2-7
isolcpus=2-7               Reserve cores 2-7 for our processes — kernel
                           won't schedule anything else there
mitigations=off            Disable Spectre/Meltdown mitigations.
                           ONLY safe if this machine runs only your code.
                           ~10-30% throughput, ~5-15 µs latency win.
                           SKIP THIS if you ever colo or share the host.
```

Adjust `2-7` based on your CPU core count. On a 16-core CPU you'd use
something like `4-15` and leave 0-3 for the OS/docker.

After reboot, verify:
```bash
cat /sys/devices/system/cpu/isolated     # → 2-7
cat /proc/cmdline                        # → contains the flags
```

**Expected gain after Phase 3:** TTT p50 from ~50 µs to ~20-30 µs.
TTT p99 from ~2-3 ms to ~200-500 µs. The big jitter win.

### Phase 4 — Pin Corsair processes to isolated cores

Docker compose can pin services to specific CPUs. Edit `docker-compose.yml`:

```yaml
services:
  corsair-broker-rs:
    cpuset: "2,3,4"          # 3 cores: main, IPC writer, dispatcher
    mem_reservation: 2g
    cap_add:
      - SYS_NICE              # allow setting RT priority inside

  trader:
    cpuset: "5,6,7"           # 3 cores: hot loop, staleness, telemetry
    mem_reservation: 1g
    cap_add:
      - SYS_NICE

  ib-gateway:
    cpuset: "0,1"             # default cores — this is JVM, low priority
```

Inside the broker/trader binaries, you could optionally set thread affinity
explicitly via `core_affinity` crate — but `cpuset` at the docker level is
~95% of the win and zero code change.

**Verify pinning:**
```bash
docker exec corsair-corsair-broker-rs-1 cat /proc/self/status | grep Cpus_allowed_list
docker exec corsair-trader-1 cat /proc/self/status | grep Cpus_allowed_list
```

### Phase 5 — IRQ pinning

Move all hardware IRQs OFF the isolated cores so they don't preempt the
hot path:

```bash
# Find your NIC IRQs
grep eth0 /proc/interrupts            # or whatever your NIC is named

# For each IRQ, write the affinity mask. To pin to cores 0-1:
echo 3 | sudo tee /proc/irq/<IRQ>/smp_affinity   # 0b00000011 = cores 0,1

# Easier: install tuned and use the network-throughput profile
sudo apt install tuned
sudo tuned-adm profile network-throughput
```

For SHM IPC (which doesn't go through the NIC), this matters less. But
when you eventually move to FIX/iLink direct, IRQ pinning is one of the
biggest wins.

**Expected gain after Phase 5:** TTT p99 from ~200-500 µs to ~100-200 µs.
You're now hitting Linux scheduler floor.

### Phase 6 — Real-time kernel (optional, only if Phase 5 isn't enough)

Ubuntu 24.04 has the `linux-image-rt` package (PREEMPT_RT kernel). This
is a major commitment — the RT kernel changes scheduler semantics and
can break poorly-written threaded code in subtle ways. Corsair is mostly
single-threaded hot path with isolated worker tasks, so it's a good
candidate.

```bash
sudo apt install linux-image-generic-hwe-24.04-rt
sudo reboot
uname -r        # should end in -realtime or -rt
```

Inside the broker/trader, set the hot-path thread to SCHED_FIFO priority
80-90 via `nix::sched::sched_setscheduler`. This requires the binary to
have SYS_NICE (already set in Phase 4).

**Expected gain after Phase 6:** TTT p99 from ~100-200 µs to ~50-100 µs.
This is the limit of what general-purpose Linux can achieve.

### Phase 7 — Kernel-bypass NIC (skip unless going colo)

Solarflare ef_vi or DPDK / io_uring SQPOLL NIC offload. ~30-50 µs win on
network RTT, but only matters when:
- you're colo'd next to the matching engine (otherwise WAN dominates)
- you're using FIX/iLink direct (the IBKR gateway is JVM and you can't
  bypass it)

For a home server: SKIP. This is a colocation-tier optimization.

---

## Part 3 — Measurement & validation

After each phase, verify the win is real before moving on. Don't tune blind.

### 3.1 Baseline measurement

The trader emits TTT (time-to-trade) histograms every 10s. Capture 1000
samples at steady state:

```bash
# In one shell:
docker compose logs -f trader 2>&1 | grep "telemetry:" | tee /tmp/ttt-baseline.log

# Let it run for 30+ minutes during US session.
# Extract p50/p99:
grep -oP 'ttt_p50_us=\d+|ttt_p99_us=\d+' /tmp/ttt-baseline.log | head
```

Or more rigorously, emit a single JSON file with summary stats:

```bash
docker exec corsair-trader-1 cat /tmp/ttt_histogram.json   # if implemented
```

Note: TTT is internal latency only (IPC receive → place_order send).
End-to-end RTT to IBKR is dominated by gateway JVM + WAN (~100-200 ms
typical for paper) and won't move with any of the tuning above.

### 3.2 Comparison table to fill in

```
                           BEFORE          AFTER           DELTA
                           (VPS)           (bare metal)
TTT p50                    ~50 µs          ___ µs          ___
TTT p99                    ~5.6 ms         ___ µs          ___
TTT max (1hr)              ~50 ms?         ___             ___
IPC send-to-receive p50    ~80 µs          ___             ___
Place order RTT mean       ~150 ms         ___             ___
                           (gateway-bound) (should be same)

Snapshot freshness         ~250 ms         ~250 ms         (unchanged)
```

The "AFTER" should show TTT p99 ≤ 200 µs after Phase 5, ≤ 100 µs after
Phase 6. If you're not seeing those numbers, something's wrong with the
tuning — don't chase further phases.

### 3.3 What NOT to over-optimize

```
DO NOT chase:
  - Place order RTT < 100 ms (gateway-bound, no fix without FIX direct)
  - Snapshot freshness < 250 ms (Streamlit poll cycle, dashboard-side)
  - SVI fit time (already 50-100 ms, runs every 60 s, not on hot path)
  - JSONL write latency (async, not on hot path)

These metrics moving means you've broken something, not that you've
optimized. They are floor-bounded by external systems.
```

---

## Part 4 — Operational continuity

### 4.1 What to do BEFORE shutting down the VPS

1. Make sure git is fully pushed: `git status && git log origin/main..main`
2. Tar up runtime state: `tar czf /tmp/corsair-runtime-$(date +%Y%m%d).tar.gz \
   .env logs-paper/ logs/ data/daily_state.json`
3. SCP to the new host: `scp /tmp/corsair-runtime-*.tar.gz new-host:~/`
4. Note the current daily P&L position. If you cut over mid-day, the
   new host's daily_state.json will resume the halt accounting from
   wherever you snapshot it.
5. Verify gateway state: the IB Gateway docker volume is empty in our
   setup (state is in container fs, not the named volume). On boot at
   the new host, IB Gateway will re-login from credentials in `.env`.

### 4.2 First boot on new host

```bash
cd ~/corsair
ls -la .env                             # must exist
docker compose build                    # ~5-10 min
docker compose up -d                    # bring up stack
sleep 30
docker compose ps                       # all should be Healthy
docker compose logs -f corsair-broker-rs | head -50
```

Watch for:
- `NativeBroker connected, clientId=0` (broker boot)
- `risk_state subscribed, listening on SHM ring` (trader boot)
- `vol_surface fit OK, F=X.XX, RMSE=Y` (broker pricing)

If you see Error 502 (IBKR can't reach the gateway), check
`docker compose logs ib-gateway` — usually this is the gateway re-logging
in and just takes 30-60 s.

### 4.3 Cutover smoke test

Before letting the new host run unattended:

```bash
# Run preflight:
docker run --rm --network host \
    -v ~/corsair/scripts:/app/scripts:ro \
    -v ~/corsair/data:/app/data:ro \
    -v ~/corsair/logs-paper:/app/logs-paper:ro \
    corsair-corsair python3 /app/scripts/cut_over_preflight.py

# Should print 9 green checks. If any are red, investigate.
# After preflight passes, watch live_monitor for 30-60 min:
docker run --rm --network host -v ~/corsair:/app:ro corsair-corsair \
    python3 /app/scripts/live_monitor.py
```

### 4.4 Rollback to VPS

If the new host has issues, the VPS can be brought back online quickly
because it's the same git tree:

1. Don't delete the VPS for at least 7 days post-cutover.
2. `docker compose stop` on new host.
3. `docker compose up -d` on VPS.
4. SCP back any logs-paper additions if you want to preserve them.

---

## Part 5 — Things you'll want to know later

```
- IBKR clientId=0 is REQUIRED on FA paper accounts. See CLAUDE.md §1.
  This is independent of host migration — same account, same gateway,
  same flag. But verify it's still set in `config/runtime_v3.yaml`
  after the move.

- The broker stores no state outside `data/` and `logs-paper/`. Host
  hostname / MAC address don't matter to IBKR.

- The trader is stateless across restarts. Its hot loop rebuilds from
  the next IPC tick.

- Gateway 2FA: if your IBKR login uses the mobile app for 2FA, you'll
  need to approve the new host's first login from the IBKR mobile app.
  This can fail silently if you're not watching the gateway logs.

- DNS: the gateway connects to IBKR via the gateway image's
  hardcoded IBKR endpoints. DNS resolution happens inside the
  container. If your home network has DNS filtering (Pi-hole etc),
  whitelist *.interactivebrokers.com and *.ibllc.com.

- Firewall: home routers often NAT outbound but block all inbound. The
  gateway needs outbound 4001/4002 (paper/live) which is fine. Inbound
  only matters if you want to dashboard from another device — port
  8501 by default, change in compose if you expose it.

- Time sync: chrony should sync to within 1 ms of UTC. Check with
  `chronyc tracking`. The fill timestamp ↔ broker timestamp comparison
  in JSONL streams will be confusing if NTP drifts > 5 ms.
```

---

## Appendix — Files at-a-glance migration checklist

```
[ ] git clone the repo on new host
[ ] copy .env (NOT in git, contains IBKR creds)
[ ] copy logs-paper/ (~1.7 GB, all v1.4 §9.5 paper streams + archive/)
[ ] copy logs/ (operational logs, optional)
[ ] copy data/daily_state.json (if mid-session)
[ ] SKIP data/corsair_ipc.* (IPC ring buffers, regenerated)
[ ] SKIP data/hg_chain_snapshot.json (regenerated 4 Hz)
[ ] SKIP rust/target/ (build artifacts)
[ ] SKIP docker images (rebuild via `docker compose build`)
[ ] SKIP corsair_ib-gateway-data volume (empty)
[ ] copy ~/.ssh/ (for git push)
[ ] copy ~/.gitconfig (for commits)
[ ] OPTIONAL: copy ~/.claude/ (this assistant's memory + history)
```

— end of handoff —
