### Title
Unsynchronized concurrent map access in `ConcurrencyLimiter`'s adaptive-limit update hook can crash Gitaly - (File: internal/limiter/concurrency_limiter.go)

### Summary
`ConcurrencyLimiter.limitsByKey` is a map that is normally only mutated under `c.m` (a `sync.RWMutex`). However, the `AfterUpdate` hook registered in `NewConcurrencyLimiter` iterates over this exact map **without acquiring `c.m`**, while it is invoked from a completely different mutex's critical section (`AdaptiveLimit`'s own `sync.Mutex`). This mirrors the reported disperser bug pattern: a shared field is guarded by one mutex in some code paths, but read/iterated elsewhere without holding that same mutex, and the two callers can run concurrently.

### Finding Description
`ConcurrencyLimiter.limitsByKey` is mutated under `c.m.Lock()`/`Unlock()` in `getConcurrencyLimit` and `putConcurrencyLimit`: [1](#0-0) [2](#0-1) 

These two functions are invoked on essentially every RPC that passes through per-RPC concurrency limiting, via `Limit()`: [3](#0-2) 

Separately, `NewConcurrencyLimiter` registers an `AfterUpdate` hook that ranges over the very same `limiter.limitsByKey` map but does **not** take `c.m`: [4](#0-3) 

This hook is invoked synchronously from `AdaptiveLimit.Update()`, which only holds `AdaptiveLimit`'s own `sync.Mutex` (unrelated to `ConcurrencyLimiter.m`): [5](#0-4) 

`Update()` is called periodically by the background `AdaptiveCalculator.calibrateLimits`, which recalibrates the limit up or down based on observed backoff conditions (e.g. load/error signals): [6](#0-5) 

Per-RPC adaptive concurrency limiting is wired up for (by default) every configured RPC via `WithConcurrencyLimiters`/`serve.go`, so `limitsByKey` map writes happen concurrently with every incoming gRPC call while the calculator can concurrently trigger `Update()` → the unguarded map range: [7](#0-6) [8](#0-7) 

Because Go's built-in `map` type is not safe for concurrent read/write, a concurrent `range` over `limitsByKey` (in the hook) racing with an insert/delete in `getConcurrencyLimit`/`putConcurrencyLimit` (triggered by ordinary RPC traffic) can trip Go's runtime concurrent-map-access detector, producing an unrecoverable `fatal error: concurrent map iteration and map write`, which terminates the entire process (this fatal error cannot be caught by `recover()`).

### Impact Explanation
This is a process-wide, unrecoverable crash (DoS) of the Gitaly daemon, not confined to a single RPC or repository. Because the race is in the concurrency-limiting middleware that wraps virtually all gRPC handlers (push, fetch, and other RPC traffic), an ordinary volume of concurrent client requests arriving while an adaptive limit is being recalibrated is sufficient to trigger it — no privileged access, malicious payload, or special crafting is required. Given Gitaly is a shared backend serving many repositories/users, taking down the process denies service to all tenants on that node.

### Likelihood Explanation
Likelihood requires: (1) adaptive concurrency limiting enabled for at least one RPC (`concurrency.Adaptive = true` in config, or a limit whose value legitimately changes over time), and (2) concurrent RPC traffic causing `getConcurrencyLimit`/`putConcurrencyLimit` map mutations to overlap with an `Update()` call. Adaptive limiting is a supported, documented Gitaly feature intended to react to load, so recalibration events happen naturally under load or backoff conditions — exactly the periods when RPC traffic (and thus concurrent map mutation) is highest, making the race more likely to manifest, not less. This is a straightforward, deterministic-once-triggered race requiring no attacker sophistication.

### Recommendation
Guard the `AfterUpdate` hook's iteration over `limiter.limitsByKey` with the same `c.m` mutex used by `getConcurrencyLimit`/`putConcurrencyLimit`, e.g., take `c.m.Lock()` (or `RLock()` if resizing individual semaphores can be done without map mutation) at the start of the hook closure before ranging over `limitsByKey`, and release it before returning. Ensure this cannot deadlock with `AdaptiveLimit`'s mutex by keeping a strict lock-ordering discipline (e.g., never call back into `AdaptiveLimit` methods while holding `c.m`).

### Proof of Concept
1. Configure Gitaly with an adaptive concurrency limit for some RPC (`concurrency.Adaptive: true`) so `NewConcurrencyLimiter`'s `AfterUpdate` hook is registered against a live `limitsByKey` map.
2. Drive sustained concurrent client RPC traffic (ordinary pushes/fetches) against that RPC across many distinct repository keys, so `getConcurrencyLimit`/`putConcurrencyLimit` are frequently inserting/deleting entries in `limitsByKey` under `c.m`.
3. Concurrently trigger conditions that cause the `AdaptiveCalculator` to recalibrate the limit (e.g., induce backoff events or let the additive-increase path run every calibration tick per `calibrateLimits`), causing `limit.Update(newLimit)` to fire and execute the unguarded `range limiter.limitsByKey` in the hook.
4. Under `go test -race` or in production under sufficient concurrency, Go's runtime will detect the concurrent map iteration/write and crash the process with `fatal error: concurrent map iteration and map write`, terminating the Gitaly daemon and dropping all in-flight RPCs.

### Citations

**File:** internal/limiter/concurrency_limiter.go (L171-184)
```go
	// When the capacity of the limiter is updated we also need to update the size of both the queuing tokens as
	// well as the concurrency tokens to match the new size.
	limit.AfterUpdate(func(val int) {
		for _, keyedLimiter := range limiter.limitsByKey {
			if keyedLimiter.queueTokens != nil {
				if semaphore, ok := keyedLimiter.queueTokens.(*resizableSemaphore); ok {
					semaphore.Resize(uint(val + limiter.maxQueueLength))
				}
			}
			if semaphore, ok := keyedLimiter.concurrencyTokens.(*resizableSemaphore); ok {
				semaphore.Resize(uint(val))
			}
		}
	})
```

**File:** internal/limiter/concurrency_limiter.go (L207-213)
```go
	if c.currentLimit() <= 0 {
		return f()
	}

	sem := c.getConcurrencyLimit(limitingKey)
	defer c.putConcurrencyLimit(limitingKey)

```

**File:** internal/limiter/concurrency_limiter.go (L245-270)
```go
func (c *ConcurrencyLimiter) getConcurrencyLimit(limitingKey string) *keyedConcurrencyLimiter {
	c.m.Lock()
	defer c.m.Unlock()

	if c.limitsByKey[limitingKey] == nil {
		// Set up the queue tokens in case a maximum queue length was requested. As the
		// queue tokens are kept during the whole lifetime of the concurrency-limited
		// function we add the concurrency tokens to the number of available token.
		var queueTokens semaphorer
		if c.maxQueueLength > 0 {
			queueTokens = c.createSemaphore(uint(c.currentLimit() + c.maxQueueLength))
		}

		c.limitsByKey[limitingKey] = &keyedConcurrencyLimiter{
			monitor:               c.monitor,
			maxQueueWait:          c.maxQueueWait,
			setWaitTimeoutContext: c.SetWaitTimeoutContext,
			concurrencyTokens:     c.createSemaphore(uint(c.currentLimit())),
			queueTokens:           queueTokens,
		}
	}

	c.limitsByKey[limitingKey].refcount++

	return c.limitsByKey[limitingKey]
}
```

**File:** internal/limiter/concurrency_limiter.go (L275-292)
```go
func (c *ConcurrencyLimiter) putConcurrencyLimit(limitingKey string) {
	c.m.Lock()
	defer c.m.Unlock()

	ref := c.limitsByKey[limitingKey]
	if ref == nil {
		panic("semaphore should be in the map")
	}

	if ref.refcount <= 0 {
		panic(fmt.Sprintf("bad semaphore ref refcount %d", ref.refcount))
	}

	ref.refcount--
	if ref.refcount == 0 {
		delete(c.limitsByKey, limitingKey)
	}
}
```

**File:** internal/limiter/adaptive_limit.go (L66-77)
```go
// Update adjusts the current limit value and executes all registered update hooks.
func (l *AdaptiveLimit) Update(val int) {
	l.Lock()
	defer l.Unlock()

	if val != l.current {
		l.current = val
		for _, hook := range l.updateHooks {
			hook(val)
		}
	}
}
```

**File:** internal/limiter/adaptive_calculator.go (L248-288)
```go
	c.stateMu.Lock()
	defer c.stateMu.Unlock()

	if ctx.Err() != nil {
		return
	}

	for _, limit := range c.limits {
		setting := limit.Setting()

		var newLimit int
		logger := c.logger.WithField("limit_rpc", limit.Name())

		if backoffEvent == nil {
			// Additive increase, one unit at a time
			newLimit = limit.Current() + 1
			if newLimit > setting.Max {
				newLimit = setting.Max
			}
			logger.WithFields(map[string]interface{}{
				"previous_limit": limit.Current(),
				"new_limit":      newLimit,
			}).Debug("Additive increase")
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
	}
```

**File:** internal/grpc/middleware/limithandler/middleware.go (L212-297)
```go
	return perRPCLimits, perRPCLimitsUnauthenticated, func(cfg config.Cfg, middleware *LimiterMiddleware) {
		acquiringSecondsMetric := prometheus.NewHistogramVec(
			prometheus.HistogramOpts{
				Namespace: "gitaly",
				Subsystem: "concurrency_limiting",
				Name:      "acquiring_seconds",
				Help:      "Histogram of time calls are rate limited (in seconds)",
				Buckets:   cfg.Prometheus.GRPCLatencyBuckets,
			},
			[]string{"system", "grpc_service", "grpc_method", "authenticated"},
		)
		inProgressMetric := prometheus.NewGaugeVec(
			prometheus.GaugeOpts{
				Namespace: "gitaly",
				Subsystem: "concurrency_limiting",
				Name:      "in_progress",
				Help:      "Gauge of number of concurrent in-progress calls",
			},
			[]string{"system", "grpc_service", "grpc_method", "authenticated"},
		)
		queuedMetric := prometheus.NewGaugeVec(
			prometheus.GaugeOpts{
				Namespace: "gitaly",
				Subsystem: "concurrency_limiting",
				Name:      "queued",
				Help:      "Gauge of number of queued calls",
			},
			[]string{"system", "grpc_service", "grpc_method", "authenticated"},
		)

		middleware.collect = func(metrics chan<- prometheus.Metric) {
			acquiringSecondsMetric.Collect(metrics)
			inProgressMetric.Collect(metrics)
			queuedMetric.Collect(metrics)
		}

		result := make(map[string]limiter.Limiter)
		resultUnauthenticated := make(map[string]limiter.Limiter)

		for _, concurrency := range cfg.Concurrency {
			// Create authenticated limiter
			result[concurrency.RPC] = limiter.NewConcurrencyLimiter(
				perRPCLimits[concurrency.RPC],
				concurrency.MaxQueueSize,
				concurrency.MaxQueueWait.Duration(),
				limiter.NewPerRPCPromMonitor(
					"gitaly", concurrency.RPC,
					queuedMetric, inProgressMetric, acquiringSecondsMetric, middleware.requestsDroppedMetric,
					true,
				),
			)

			// Create unauthenticated limiter if configured
			if adaptiveLimit, ok := perRPCLimitsUnauthenticated[concurrency.RPC]; ok {
				unauthLimits := concurrency.Unauthenticated
				resultUnauthenticated[concurrency.RPC] = limiter.NewConcurrencyLimiter(
					adaptiveLimit,
					unauthLimits.MaxQueueSize,
					unauthLimits.MaxQueueWait.Duration(),
					limiter.NewPerRPCPromMonitor(
						"gitaly", concurrency.RPC,
						queuedMetric, inProgressMetric, acquiringSecondsMetric, middleware.requestsDroppedMetric,
						false,
					),
				)
			}
		}

		// Set default for ReplicateRepository.
		replicateRepositoryFullMethod := "/gitaly.RepositoryService/ReplicateRepository"
		if _, ok := result[replicateRepositoryFullMethod]; !ok {
			result[replicateRepositoryFullMethod] = limiter.NewConcurrencyLimiter(
				limiter.NewAdaptiveLimit("staticLimit", limiter.AdaptiveSetting{Initial: 1}),
				0,
				0,
				limiter.NewPerRPCPromMonitor(
					"gitaly", replicateRepositoryFullMethod,
					queuedMetric, inProgressMetric, acquiringSecondsMetric, middleware.requestsDroppedMetric,
					true,
				),
			)
		}

		middleware.methodLimiters = result
		middleware.methodLimitersUnauthenticated = resultUnauthenticated
	}
```

**File:** internal/cli/gitaly/serve.go (L349-371)
```go
	// List of tracking adaptive limits. They will be calibrated by the adaptive calculator
	adaptiveLimits := []limiter.AdaptiveLimiter{}

	perRPCLimits, perRPCLimitsUnauthenticated, setupPerRPCConcurrencyLimiters := limithandler.WithConcurrencyLimiters(cfg)
	for _, concurrency := range cfg.Concurrency {
		// Connect adaptive limits to the adaptive calculator
		if concurrency.Adaptive {
			if limit, ok := perRPCLimits[concurrency.RPC]; ok {
				adaptiveLimits = append(adaptiveLimits, limit)
			}
		}
		if concurrency.Unauthenticated.Adaptive {
			if unauthLimit, ok := perRPCLimitsUnauthenticated[concurrency.RPC]; ok {
				adaptiveLimits = append(adaptiveLimits, unauthLimit)
			}
		}
	}
	perRPCLimitHandler := limithandler.New(
		cfg,
		limithandler.LimitConcurrencyByRepo,
		setupPerRPCConcurrencyLimiters,
	)
	prometheus.MustRegister(perRPCLimitHandler)
```
