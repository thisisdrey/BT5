## Analog Found: Burden-monitor CPU/Memory accounting undercounts evicted-but-still-running commands, weakening `LoadShedder`'s DoS protection

### Title
Eviction of unpolled commands from `RPCEntry.Commands` lets an attacker hide an in-flight RPC's true resource usage from the load shedder - ([File: internal/burdenmonitor/rpc_entry.go])

### Summary
`RPCEntry` caps the number of concurrently tracked git subprocess entries at `maxTrackedCommands` (20) and evicts the "lowest CPU consumption" entry to make room for new ones [1](#0-0) . `TotalCPUTime()`/`TotalMemory()`/`ActiveCommandCount()` only sum over the currently-tracked `e.Commands` map plus the already-folded `completedCPUTime` [2](#0-1) , and `LoadShedder.shed()` selects which RPCs to cancel strictly by this `TotalCPUTime()`/`TotalMemory()` ranking [3](#0-2) . Just like the Vault bug where `totalAllocatedTokens` was not reduced when a protocol's `currentAllocations` was zeroed out (leaving a stale, inflated total that skews downstream allocation math), here an eviction removes a command's stats from the live tracking structure without folding its accrued cost into the aggregate at eviction time, and the aggregate only becomes correct again once (and if) the evicted command's completion callback eventually fires.

### Finding Description
`RegisterCommand` evicts a victim once the per-RPC cap is reached, picking the entry with the currently lowest `UserTime+SystemTime` [4](#0-3) . A newly registered `CommandStats` starts with zero `UserTime`/`SystemTime` because these fields are only populated by the periodic poller running every 2 seconds [5](#0-4) [6](#0-5) . Consequently, a command that is genuinely expensive but was registered less than one poll interval ago is indistinguishable (CPU=0) from other recently started, cheap commands, and can be selected as the eviction victim.

Once evicted, `delete(e.Commands, victim.Pid)` removes the entry from the map that both `TotalCPUTime()`/`TotalMemory()` and `pollEntryCommands` iterate over [7](#0-6) [6](#0-5) . The poller can no longer update its resource usage, so its real, ongoing CPU/memory consumption is invisible to the burden monitor until the process actually exits and `NotifyCommandCompleted`/`MarkCommandCompleted` is invoked, which folds the final numbers into `completedCPUTime` [8](#0-7) [9](#0-8) .

An ordinary user issuing a single RPC that spawns a burst of more than `maxTrackedCommands` (20) inexpensive git subprocesses in quick succession (all of them registering with zero recorded CPU before the next poll tick) can force the eviction logic to repeatedly discard whichever entries happen to be picked as "lowest," including a genuinely expensive, still-running process for that same RPC. That process then silently disappears from `TotalCPUTime()`/`TotalMemory()`/`ActiveCommandCount()` for the remainder of its execution.

### Impact Explanation
`LoadShedder.shed()` is the DoS self-protection mechanism triggered on critical PSI/OOM pressure; it fetches `GetTopNEntries(shedTopN, SortByCPU)` and cancels those RPCs [3](#0-2) . Because the true resource hog can be made invisible to `TotalCPUTime()`, it may never appear in the top-N ranking, so the load shedder cancels other, less costly RPCs instead of the actual offender, undermining the intended overload-protection guarantee. This is analogous to the Vault case: a stale/incomplete accounting value silently distorts a security- or fairness-relevant decision downstream (there, token allocation; here, which RPC gets killed under load), and no privileged access is required to trigger it — any user's crafted RPC that spawns enough short-lived subprocesses suffices.

### Likelihood Explanation
Any unauthenticated-at-this-layer client capable of invoking a Gitaly RPC that spawns many git subprocesses (e.g., an operation touching many refs/objects that shells out repeatedly) can trigger the eviction path, since `maxTrackedCommands` is a fixed constant (20) reachable during ordinary heavy usage, not just adversarial usage [1](#0-0) . The precise selection of victim depends on poll timing (2s interval) and Go map iteration order among tied zero-CPU entries, so reliably targeting a specific process requires some timing effort, but the general effect (hiding growing resource usage from the shedder for a window) is straightforward to reproduce.

### Recommendation
When evicting a command from `e.Commands`, immediately fold its last known `UserTime+SystemTime` into `completedCPUTime` (as a provisional credit) rather than only doing so on eventual `MarkCommandCompleted`, or exclude eviction candidates whose stats have never been polled (i.e., require at least one poll cycle before an entry becomes eviction-eligible) so that genuinely large, still-running consumers cannot be pushed out of tracking while they continue to accrue cost. Alternatively, track an RPC-level running total (updated on every poll for all commands regardless of whether they later get evicted) independent of the bounded `Commands` map, similar to fixing `totalAllocatedTokens` at the moment of the state change rather than only at future events.

### Proof of Concept
1. A client issues an RPC that internally forks a genuinely CPU-heavy git subprocess (pid P1), immediately followed by ≥20 more trivial git subprocess invocations within the same RPC context, all registered before the 2-second poller tick fires.
2. `RegisterCommand` is called for each new subprocess; once `len(e.Commands) >= maxTrackedCommands`, `evictCommand()` compares `UserTime+SystemTime` across all current entries — all of which read `0` because none have been polled yet — and (depending on map iteration order) evicts P1's entry via `delete(e.Commands, victim.Pid)` [7](#0-6) .
3. From this point on, `pollEntryCommands` no longer sees P1 (it iterates only `entry.Commands`) [6](#0-5) , so `entry.TotalCPUTime()` no longer reflects P1's growing CPU usage [10](#0-9) .
4. If a PSI/OOM critical condition fires while P1 is still running, `LoadShedder.shed()` ranks RPCs by `TotalCPUTime()` and will not select this RPC for cancellation despite it being the true resource consumer, instead cancelling other RPCs whose usage is still fully visible [3](#0-2) .

This report matches the requested "RPC-handler resource limits" analog category: an ordinary user's crafted RPC (burst of subprocesses) causes an internal accounting variable to fall out of sync with real state, degrading a resource-limiting/DoS-protection handler exactly as the original Vault report describes for token-allocation accounting.

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

**File:** internal/burdenmonitor/rpc_entry.go (L62-92)
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

// TotalMemory returns the sum of anonymous RSS memory across all commands.
func (e *RPCEntry) TotalMemory() int64 {
	e.mu.RLock()
	defer e.mu.RUnlock()

	var total int64
	for _, cmd := range e.Commands {
		total += cmd.AnonRSS
	}
	return total
}

// ActiveCommandCount returns the number of commands that have not yet completed.
func (e *RPCEntry) ActiveCommandCount() int {
	e.mu.RLock()
	defer e.mu.RUnlock()

	return len(e.Commands)
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

**File:** internal/burdenmonitor/poller.go (L12-14)
```go
const (
	defaultPollInterval = 2 * time.Second
	defaultLogInterval  = 5 * time.Minute
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
