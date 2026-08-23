### Title
Adaptive concurrency limit can decay to zero, silently disabling RPC concurrency limiting - (File: internal/limiter/adaptive_calculator.go, internal/limiter/concurrency_limiter.go)

### Summary
The `AdaptiveCalculator` reduces a per-RPC concurrency limit multiplicatively during backoff events (e.g. CPU throttling, memory pressure), and `ConcurrencyLimiter.Limit` treats a limit of `0` (or less) as "no concurrency limiting" rather than "block everything." If the calculated limit decays to `0` while the configured `Min` is `0` (the zero-value default when `min_limit` is not explicitly configured), the concurrency limiter is completely disabled at exactly the moment the system is under resource pressure — the opposite of the intended backoff behavior. This mirrors the "unchecked zero-value" bug class from the report (fee silently becoming `0` due to unfavorable rounding, resulting in a bypass of an intended check).

### Finding Description
`AdaptiveCalculator.calibrateLimits` computes the new limit on a backoff event as: [1](#0-0) 

`newLimit = floor(limit.Current() * BackoffFactor)`, clamped only to `setting.Min`. `AdaptiveSetting.Min` defaults to the Go zero value `0` if `min_limit` is not set in the `[[concurrency]]` config block: [2](#0-1) 

With `DefaultBackoffFactor = 0.5` [3](#0-2) , if the current limit is `1` (which can be reached after repeated backoff events, or if `InitialLimit`/`MinLimit` were configured low), `floor(1 * 0.5) = 0`. Since `Min` defaults to `0`, this `0` is accepted as valid and propagated via `Update(0)` to the `ConcurrencyLimiter`.

Critically, `ConcurrencyLimiter.Limit` special-cases a non-positive limit as "unlimited," not "blocked": [4](#0-3) 

So once the adaptive limit reaches `0`, every subsequent call to `Limit()` for that RPC bypasses the concurrency queue/semaphore entirely and executes `f()` unconditionally — precisely while the `LoadMonitor` has signaled CPU throttling, memory pressure, or PSI-based resource exhaustion (the very conditions the adaptive limiter exists to protect against).

### Impact Explanation
This converts a protective backpressure mechanism into a bypass under load. During sustained backoff events (e.g. an attacker-driven surge of `PostUploadPackWithSidechannel`/clone-fetch requests causing CPU/memory pressure), the concurrency limiter for that RPC can decay to `0` and then admit unbounded concurrent requests with no queueing or rejection, defeating the purpose documented in `doc/backpressure.md` and `doc/load-management-architecture.md` (limiter is supposed to reject/queue requests exceeding capacity, not open the floodgates). This can amplify resource exhaustion (CPU, memory, file descriptors, git subprocess spawning) into a full denial-of-service of the Gitaly node, exactly at the moment it is least able to handle it.

### Likelihood Explanation
Reaching a limit of `1` before another backoff event requires either a low `initial_limit`/`min_limit` configuration or several consecutive halvings after repeated pressure events on a lightly-provisioned/low-limit RPC. Since `min_limit` is optional and defaults to `0`, operators who configure `adaptive = true` without explicitly setting `min_limit` to a positive value are exposed. This is triggerable purely by ordinary client load patterns (repeated fetch/clone bursts) that induce sustained CPU/memory pressure — no privileged access or malicious peer behavior is required, only a normal but heavy pattern of push/fetch/clone traffic that the load monitor already treats as a backoff condition.

### Recommendation
Enforce a floor above zero for adaptive concurrency limits:
- In `AdaptiveCalculator.calibrateLimits`, ensure `newLimit` never drops to `0` unless `Min` is explicitly and intentionally configured to `0` with a clear semantic meaning (or reject `Min == 0` for adaptive settings that are meant to represent "at least 1 concurrent slot").
- In `ConcurrencyLimiter.Limit`, distinguish "no limiting configured" (e.g. `nil`/sentinel "unset" limit) from "limit calculated as 0" so that a computed `0` from the adaptive calculator is honored as "block all" (or clamped to `1`) rather than silently disabling limiting via the `currentLimit() <= 0` bypass at [4](#0-3) .
- Add config validation requiring `min_limit >= 1` whenever `adaptive = true` is set, similar to existing validation in `internal/gitaly/config/config_test.go`'s `TestConcurrency_Validate`.

### Proof of Concept
1. Configure an adaptive concurrency limit without `min_limit` (defaults to `0`):
```toml
[[concurrency]]
rpc = "/gitaly.SmartHTTPService/PostUploadPackWithSidechannel"
adaptive = true
initial_limit = 2
max_limit = 10
max_queue_size = 100
```
2. Drive sustained CPU/memory pressure via repeated legitimate fetch/clone requests until the `LoadMonitor` fires two consecutive backoff events (`condition_cpu_throttling` / `condition_memory_anon`), each halving the limit: `2 → 1 → 0` (per `calibrateLimits`, `internal/limiter/adaptive_calculator.go:271-276`).
3. Once `AdaptiveLimit.Current()` reaches `0`, every subsequent call to `ConcurrencyLimiter.Limit` takes the `currentLimit() <= 0` branch (`internal/limiter/concurrency_limiter.go:207-209`) and executes the handler unconditionally, with no queueing and no rejection, regardless of how many concurrent requests arrive — restoring unlimited concurrency for that RPC precisely while the node is under resource pressure.

### Citations

**File:** internal/limiter/adaptive_calculator.go (L23-25)
```go
	// DefaultBackoffFactor is the default recommended backoff factor when the concurrency decreases. By default,
	// the factor is 0.5, meaning the limit is cut off by half when a backoff event occurs.
	DefaultBackoffFactor = 0.5
```

**File:** internal/limiter/adaptive_calculator.go (L271-276)
```go
		} else {
			// Multiplicative decrease
			newLimit = int(math.Floor(float64(limit.Current()) * setting.BackoffFactor))
			if newLimit < setting.Min {
				newLimit = setting.Min
			}
```

**File:** internal/limiter/adaptive_limit.go (L7-13)
```go
// AdaptiveSetting is a struct that holds the configuration parameters for an adaptive limiter.
type AdaptiveSetting struct {
	Initial       int
	Max           int
	Min           int
	BackoffFactor float64
}
```

**File:** internal/limiter/concurrency_limiter.go (L207-209)
```go
	if c.currentLimit() <= 0 {
		return f()
	}
```
