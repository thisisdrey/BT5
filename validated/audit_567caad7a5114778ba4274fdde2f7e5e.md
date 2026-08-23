### Title
Burden monitor undercounts evicted commands' CPU time, allowing load-shedding bypass - ([File: internal/burdenmonitor/rpc_entry.go])

### Summary
`RPCEntry.evictCommand()` discards a tracked command's accrued CPU-time record without folding it into the entry's aggregate `completedCPUTime`, mirroring the "overwrite instead of accumulate" bug class from the referenced report: a per-item value is dropped/overwritten instead of being added to a running total that gates a resource decision.

### Finding Description
`RPCEntry.Commands` is capped at `maxTrackedCommands = 20` [1](#0-0) . When a new command is registered and the cap is already reached, `RegisterCommand` calls `evictCommand()` to make room [2](#0-1) .

`evictCommand()` selects the tracked command with the lowest `UserTime+SystemTime` and simply removes it from the map with `delete(e.Commands, victim.Pid)`, incrementing only a Prometheus eviction counter — it never adds `victim.UserTime+victim.SystemTime` into `e.completedCPUTime`: [3](#0-2) 

The comment claims "Its resource consumption will be added to the aggregate during the `MarkCommandCompleted` call," but `MarkCommandCompleted` receives the command's *final* userTime/systemTime independently from the caller (via `NotifyCommandCompleted`) and simply does `e.completedCPUTime += userTime + systemTime`: [4](#0-3) [5](#0-4) 

This *is* consistent as long as `MarkCommandCompleted` is reliably invoked for every spawned process with correct final CPU numbers. However, once a command is evicted from `e.Commands`, `pollEntryCommands()` (the periodic `/proc` poller) no longer iterates over it, since it only ranges over `entry.Commands`: [6](#0-5) 

So `TotalCPUTime()` — which is `completedCPUTime` plus the sum of currently-tracked `Commands`' `UserTime+SystemTime` — undercounts an RPC's real, currently accruing CPU usage for the whole time an evicted-but-still-running command keeps consuming CPU, and its accrued time recorded at the moment of eviction is discarded rather than accumulated: [7](#0-6) 

### Impact Explanation
`TotalCPUTime()` is the sole signal the `LoadShedder` uses to rank and cancel the costliest in-flight RPCs during resource-pressure events (`GetTopNEntries(shedTopN, SortByCPU)`): [8](#0-7) 

Because eviction silently drops accrued CPU time and stops future polling of that process, an ordinary client can issue an accessor RPC that spawns more than `maxTrackedCommands` (20) Git subprocesses (e.g., an RPC that fans out many `git cat-file`/`git log` invocations per input item) to make the entry's *reported* CPU usage stay artificially low relative to its *actual* resource consumption. This directly weakens the DoS-mitigation mechanism (`LoadShedder`) that is supposed to detect and cancel the heaviest RPCs under CPU/memory/IO pressure, since a heavy RPC can rank artificially low on the CPU leaderboard and escape being shed, worsening host resource exhaustion — a DoS-of-handler-limits condition matching the "RPC-handler resource limits" analog class.

### Likelihood Explanation
Triggering eviction only requires an ordinary, unprivileged client to call any accessor-type RPC that spawns more than 20 concurrently tracked Git subprocesses (the interceptor tracks all `OpAccessor` methods when `featureflag.BurdenMonitorTrackCommands` is enabled) [9](#0-8) . No special privileges, crafted binary payloads, or race conditions are needed — a request that legitimately fans out many short-lived git processes is sufficient, matching the comment's own acknowledgement that "some RPCs spawn a variable number of Git commands depending on the shape of the user input" [10](#0-9) .

### Recommendation
In `evictCommand()`, fold the victim's already-accrued `UserTime+SystemTime` into `e.completedCPUTime` (and its `AnonRSS` accounting if memory totals should also survive eviction) at the moment of eviction, rather than relying solely on a later `MarkCommandCompleted` call that no longer receives live updates for the evicted PID:

```go
func (e *RPCEntry) evictCommand() {
    var victim *CommandStats
    for _, cmd := range e.Commands {
        if victim == nil || cmd.UserTime+cmd.SystemTime < victim.UserTime+victim.SystemTime {
            victim = cmd
        }
    }
    if victim == nil {
        return
    }

    e.completedCPUTime += victim.UserTime + victim.SystemTime
    delete(e.Commands, victim.Pid)

    commandsEvictedTotal.WithLabelValues(e.ServiceName, e.MethodName).Inc()
}
```
Additionally, consider tracking evicted-but-still-running PIDs so their subsequent, larger final CPU numbers (from `MarkCommandCompleted`) are not silently added on top of stale/incomplete accounting, or explicitly document/guard against double counting.

### Proof of Concept
1. Issue an accessor RPC (with `featureflag.BurdenMonitorTrackCommands` enabled) whose handler spawns more than `maxTrackedCommands` (20) Git subprocesses concurrently, e.g., by requesting per-path/per-ref data for >20 distinct paths/refs in one call.
2. As each new command beyond the 20th is registered via `NotifyCommandStarted` → `RegisterCommand`, `evictCommand()` removes the lowest-CPU tracked command from `entry.Commands` without adding its CPU time to `completedCPUTime` [11](#0-10) .
3. That evicted process continues running and consuming CPU, but `pollEntryCommands()` no longer updates its `UserTime`/`SystemTime` since it iterates only `entry.Commands` [6](#0-5) .
4. Observe via `entry.TotalCPUTime()` (or the `LoadShedder`'s `GetTopNEntries(shedTopN, SortByCPU)` ranking) that the RPC's reported CPU usage is significantly lower than its actual usage, allowing it to avoid being ranked/cancelled during a load-shedding event [8](#0-7) .

### Citations

**File:** internal/burdenmonitor/rpc_entry.go (L20-27)
```go
// maxTrackedCommands caps how many commands an RPC entry tracks at once. A
// survey of RPCs yielded at most 10 distinct Git commands. Some Git commands
// are accompanied by a git-rev-parse(1) invocation to detect the hash format.
//
// Some RPCs spawn a variable number of Git commands depending on the shape of
// the user input. Those are typically very short-lived commands, so excluding
// the cheaper ones should not affect burden monitoring.
const maxTrackedCommands = 20
```

**File:** internal/burdenmonitor/rpc_entry.go (L62-72)
```go
// TotalCPUTime returns the sum of user and system CPU time across running commands.
func (e *RPCEntry) TotalCPUTime() time.Duration {
	e.mu.RLock()
	defer e.mu.RUnlock()

	total := e.completedCPUTime
	for _, cmd := range e.Commands {
		total += cmd.UserTime + cmd.SystemTime
	}
	return total
}
```

**File:** internal/burdenmonitor/rpc_entry.go (L107-140)
```go
// RegisterCommand adds a new command to the RPC entry's tracking.
func (e *RPCEntry) RegisterCommand(pid int, name string, startTime time.Time) {
	e.mu.Lock()
	defer e.mu.Unlock()

	if _, ok := e.Commands[pid]; !ok && len(e.Commands) >= maxTrackedCommands {
		e.evictCommand()
	}

	e.Commands[pid] = &CommandStats{
		Name:      name,
		Pid:       pid,
		StartTime: startTime,
	}
}

// evictCommand removes the tracked command with the lowest CPU consumption.
// Its resource consumption will be added to the aggregate during the
// MarkCommandCompleted call.
func (e *RPCEntry) evictCommand() {
	var victim *CommandStats
	for _, cmd := range e.Commands {
		if victim == nil || cmd.UserTime+cmd.SystemTime < victim.UserTime+victim.SystemTime {
			victim = cmd
		}
	}
	if victim == nil {
		return
	}

	delete(e.Commands, victim.Pid)

	commandsEvictedTotal.WithLabelValues(e.ServiceName, e.MethodName).Inc()
}
```

**File:** internal/burdenmonitor/rpc_entry.go (L142-152)
```go
// MarkCommandCompleted removes a completed command from the entry's tracking,
// folding its final CPU times into the entry's aggregates.
func (e *RPCEntry) MarkCommandCompleted(pid int, userTime, systemTime time.Duration) {
	e.mu.Lock()
	defer e.mu.Unlock()

	delete(e.Commands, pid)

	e.completedCPUTime += userTime + systemTime
	e.completedCount++
}
```

**File:** internal/burdenmonitor/interceptor.go (L38-47)
```go
// NotifyCommandCompleted notifies the burden monitor that a command has completed.
// This should be called when a command finishes execution.
func NotifyCommandCompleted(ctx context.Context, pid int, userTime, systemTime time.Duration) {
	entry, ok := rpcEntryFromContext(ctx)
	if !ok {
		return
	}

	entry.MarkCommandCompleted(pid, userTime, systemTime)
}
```

**File:** internal/burdenmonitor/interceptor.go (L49-77)
```go
// UnaryInterceptor returns a gRPC unary server interceptor that tracks RPC execution.
func (bm *BurdenMonitor) UnaryInterceptor() grpc.UnaryServerInterceptor {
	return func(ctx context.Context, req interface{}, info *grpc.UnaryServerInfo, handler grpc.UnaryHandler) (interface{}, error) {
		if featureflag.BurdenMonitorTrackCommands.IsEnabled(ctx) {
			mi, err := protoregistry.GitalyProtoPreregistered.LookupMethod(info.FullMethod)
			if err != nil {
				// The gRPC health checks calls are not part of the Gitaly proto
				// registry, yet they are legitimate requests. Let's not log
				// everytime a health check is received as it pollutes the logs,
				// mostly when using Praefect.
				if !strings.Contains(info.FullMethod, grpchealth.Health_ServiceDesc.ServiceName) {
					bm.logger.WithError(err).Warn("burden monitor stream interceptor: unable to lookup method info")
				}
				return handler(ctx, req)
			}

			// Only track accessor method in the burden monitor
			if mi.Operation == protoregistry.OpAccessor {
				ctx, entry := bm.RegisterRPC(ctx, info.FullMethod)

				defer bm.DeregisterRPC(entry.ID)
				return handler(ctx, req)

			}
		}

		return handler(ctx, req)
	}
}
```

**File:** internal/burdenmonitor/poller.go (L107-125)
```go
func (bm *BurdenMonitor) pollEntryCommands(entry *RPCEntry) {
	entry.mu.Lock()
	defer entry.mu.Unlock()

	for pid, cmd := range entry.Commands {
		stats, err := readProcessStats(pid)
		if err != nil {
			bm.logger.WithFields(log.Fields{
				"pid":   pid,
				"error": err,
			}).DebugContext(entry.Context, "failed to read process stats")
			continue
		}

		cmd.UserTime = stats.UserTime
		cmd.SystemTime = stats.SystemTime
		cmd.WallTime = time.Since(cmd.StartTime)
		cmd.AnonRSS = stats.AnonRSS
	}
```

**File:** internal/burdenmonitor/load_shedder.go (L114-127)
```go
func (ls *LoadShedder) shed(ctx context.Context, event loadmonitor.Event) {
	entries := ls.bm.GetTopNEntries(shedTopN, SortByCPU)
	reason := SortByCPU.reason()

	for _, entry := range entries {
		ls.cancel(entry, reason)
	}

	ls.logger.WithFields(log.Fields{
		"condition":    event.ConditionName,
		"reason":       event.Description,
		"sniped_count": len(entries),
	}).WarnContext(ctx, "load shedder cancelled in-flight RPCs")
}
```
