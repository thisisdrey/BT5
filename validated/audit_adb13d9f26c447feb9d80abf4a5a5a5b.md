### Title
Adaptive concurrency limit can decay to zero and fully disables RPC throttling exactly when the node is under attacker-induced resource pressure - (File: internal/limiter/concurrency_limiter.go, internal/limiter/adaptive_calculator.go)

### Summary
Gitaly's adaptive concurrency-limiting subsystem uses an AIMD algorithm: on each calibration tick the limit either increases by one or, on a resource-pressure "backoff event," is multiplied by `BackoffFactor` (default `0.5`) and floored via `int(math.Floor(...))`. When the computed limit reaches `0` (or the configured `Min` is `0`), the `ConcurrencyLimiter.Limit` function contains a guard that treats a non-positive current limit as "no limiting configured" and executes the request function directly, bypassing all concurrency and queueing controls. An unprivileged client that can repeatedly drive the node into resource pressure (e.g., via large/expensive pushes, fetches, or pack-objects requests) can therefore cause the adaptive limit for a given RPC to decay to zero, at which point Gitaly's own protection mechanism turns itself off completely, allowing unlimited concurrent admission of that RPC precisely when the system is already overloaded.

### Finding Description
The `AdaptiveCalculator.calibrateLimits` function implements the AIMD control loop: [1](#0-0) 

On a backoff event, the new limit is `int(math.Floor(float64(limit.Current()) * setting.BackoffFactor))`, clamped only by `setting.Min`. With the documented default `BackoffFactor = 0.5`: [2](#0-1) 

If `Min` is configured (or defaults) to `0`, or even if `Min` is small, a sustained sequence of backoff events (sustained CPU/memory/PSI pressure) will floor the limit down toward `0` — e.g. `1 * 0.5 = 0.5 → floor → 0`. Repeated real-world resource pressure — which an attacker can induce by issuing costly Git operations (large clones/pushes/pack-objects requests that consume CPU, memory, or trigger PSI pressure watchers) — will keep driving the value down each calibration cycle.

Once the resulting `AdaptiveLimit.Current()` is `0`, `ConcurrencyLimiter.Limit` treats this as "limiting disabled": [3](#0-2) 

This check (`if c.currentLimit() <= 0 { return f() }`) is normally meant to represent "no concurrency limiting configured" (i.e., an administrator explicitly setting a limit of `0` to disable limiting for an RPC). However, because the same field is also the live output of the adaptive calculator, a limit that decays to `0` due to genuine (or attacker-amplified) system pressure produces the exact same effect: the RPC becomes completely unthrottled, with no queueing and no concurrency cap, at the moment the system is least able to handle unbounded concurrency.

This inverts the intended protection: the concurrency limiter is supposed to shed load and queue/reject excess requests during high-pressure conditions (per the AIMD design and the `load-management-architecture.md` documentation describing "Multiplicative decrease" and load shedding), but the specific floor-to-zero code path instead removes all admission control for that RPC. [4](#0-3) 

### Impact Explanation
For any RPC governed by an adaptive concurrency limiter (configured via `cfg.Concurrency` and wired through `WithConcurrencyLimiters`), an ordinary/unprivileged client that can generate enough sustained load to trigger repeated backoff events can cause the limiter for that RPC (or globally, since the same `AdaptiveCalculator` instance can govern multiple `AdaptiveLimiter`s moving together) to collapse to zero and thereby fully disable admission control. Once disabled, an unlimited number of concurrent requests for that RPC are admitted directly to the handler with no queueing and no per-key semaphore cap, amplifying exactly the resource exhaustion condition that triggered the backoff in the first place. This is a genuine denial-of-service amplification vector against an RPC-handler resource-limiting mechanism, reachable purely from client-controllable request volume/cost, without any privileged access.

### Likelihood Explanation
The precondition is a sustained sequence of backoff events, which requires actual system resource pressure (CPU throttling, memory pressure, PSI thresholds) to be crossed repeatedly across calibration intervals (`DefaultCalibrateFrequency = 30s`). An attacker able to issue costly Git operations (large pushes, clones, or repeated pack-objects invocations) can plausibly sustain such pressure over several calibration cycles, especially since each additive-increase step only restores `+1` per cycle while each backoff event halves the limit — the decay is fast (log2) while recovery is linear, making it comparatively easy to keep the limit pinned near zero. Whether `Min` is configured as `0` in default/typical deployments determines whether the limit can reach exactly `0` (fully disabling the limiter) versus merely a low positive floor (severe but not complete disablement); this configuration detail could not be fully confirmed from the available index.

### Recommendation
- Separate the semantics of "administrator explicitly disabled limiting" from "adaptive limit decayed to a low value." Do not use `current <= 0` as a sentinel for "unlimited" when the value is driven by the adaptive calculator.
- Enforce a strictly positive floor (`Min >= 1`) for any `AdaptiveSetting` used by the calculator, and validate this at configuration load time, so the multiplicative decrease can never reach zero for adaptively-managed limits.
- Consider making the zero-disables-limiting behavior only apply to statically configured (non-adaptive) limiters, using a distinct flag rather than overloading the numeric value.

### Proof of Concept
Conceptual reproduction (cannot be executed against this environment, based on code-path analysis):
1. Configure an RPC with an adaptive concurrency limit, e.g. `Initial: 20, Max: 100, Min: 0, BackoffFactor: 0.5` (mirrors the test fixtures in `adaptive_calculator_test.go`), reachable in production via `cfg.Concurrency` entries.
2. As an unprivileged client, repeatedly issue expensive operations against that RPC (e.g., large `git-upload-pack`/`git-receive-pack` traffic or `PackObjects` calls) sufficient to keep tripping the configured `CgroupCPUWatcher`/`CgroupMemoryWatcher`/PSI condition thresholds across multiple `30s` calibration ticks.
3. Each tick with an active backoff event calls `calibrateLimits`, producing `newLimit = floor(current * 0.5)`, converging to `0` after `~log2(Initial)` consecutive backoff cycles (e.g., 5 cycles from an initial limit of 20, per `adaptive_calculator_test.go`'s "a series of backoff events" case which reaches the configured minimum).
4. Once `AdaptiveLimit.Current()` reaches `0`, all subsequent calls to `ConcurrencyLimiter.Limit` for that RPC hit the `c.currentLimit() <= 0` branch and execute `f()` immediately, with no concurrency cap or queue — the limiter is now a no-op precisely while the underlying resource pressure watchers indicate the system is still overloaded.

### Citations

**File:** internal/limiter/adaptive_calculator.go (L23-25)
```go
	// DefaultBackoffFactor is the default recommended backoff factor when the concurrency decreases. By default,
	// the factor is 0.5, meaning the limit is cut off by half when a backoff event occurs.
	DefaultBackoffFactor = 0.5
```

**File:** internal/limiter/adaptive_calculator.go (L271-287)
```go
		} else {
			// Multiplicative decrease
			newLimit = int(math.Floor(float64(limit.Current()) * setting.BackoffFactor))
			if newLimit < setting.Min {
				newLimit = setting.Min
			}
			fields := make(map[string]interface{}, len(backoffStatFields)+4)
			for key, value := range backoffStatFields {
				fields[key] = value
			}
			fields["previous_limit"] = limit.Current()
			fields["new_limit"] = newLimit
			fields["condition"] = backoffEvent.ConditionName
			fields["reason"] = backoffEvent.Reason
			logger.WithFields(fields).Info("Multiplicative decrease")
		}
		c.updateLimit(limit, newLimit)
```

**File:** internal/limiter/concurrency_limiter.go (L197-209)
```go
func (c *ConcurrencyLimiter) Limit(ctx context.Context, limitingKey string, f LimitedFunc) (interface{}, error) {
	span, ctx := tracing.StartSpanIfHasParent(
		ctx,
		"limiter.ConcurrencyLimiter.Limit",
		[]attribute.KeyValue{
			attribute.String("key", limitingKey),
		},
	)
	defer span.End()

	if c.currentLimit() <= 0 {
		return f()
	}
```

**File:** doc/load-management-architecture.md (L150-165)
```markdown
            alt Healthy
                LM->>AC: No event
                AC->>CL: limit += 1
            else Backoff severity
                LM->>AC: BackoffEvent
                AC->>CL: limit = floor(limit x 0.5)
            else Critical severity
                LM->>AC: BackoffEvent
                AC->>CL: limit = floor(limit x 0.5)
                LM->>LS: CriticalEvent
                LS->>BM: EntriesSortedBy(CPU)
                BM-->>LS: Ranked in-flight RPCs
                LS->>BM: SnipeTopN()
                BM-->>Client: RESOURCE_EXHAUSTED (cancel highest-cost)
            end
        end
```
