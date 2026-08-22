### Title
`findWheel` maps `timeout == wheelDuration` onto `tw.current` when `wheelDuration % tickDuration != 0`, causing immediate conntrack eviction - ([File: timeout.go])

### Summary
`TimerWheel.findWheel` computes the target slot as `ceil(timeout/tickDuration) + tw.current + 1`, wrapped modulo `wheelLen = floor(max/min)+2`. When the requested `timeout` equals the wheel's configured maximum (`tw.wheelDuration`) and that maximum is not an exact multiple of `tw.tickDuration`, the wrap-around arithmetic collapses to `tick == tw.current`, i.e. the item is placed in the slot currently being processed instead of a future one. The very next `Advance()` call sweeps it straight into `tw.expired`, and `Purge`/`evict` removes the conntrack entry essentially immediately after creation.

### Finding Description
`findWheel` (timeout.go:164-184) first clamps `timeout` to `[tickDuration, wheelDuration]`, then computes:

```go
tick := int(((timeout - 1) / tw.tickDuration) + 1)   // ceil(timeout/tickDuration)
tick += tw.current + 1
if tick >= tw.wheelLen {
    tick -= tw.wheelLen
}
``` [1](#0-0) 

`wheelLen` is computed in `NewTimerWheel` as `wLen := int((max / min) + 2)` [2](#0-1) , which is `floor(max/min) + 2` using Go's integer division.

For `timeout == max` (`wheelDuration`), `tick_pre = ceil(max/min)`. When `max` is **not** an exact multiple of `min`, `ceil(max/min) = floor(max/min) + 1 = wheelLen - 1`. Substituting into the offset:

```
total = tick_pre + current + 1 = (wheelLen - 1) + current + 1 = wheelLen + current
tick  = total - wheelLen = current      // single subtraction, since total < 2*wheelLen
```

So `findWheel(wheelDuration)` returns exactly `tw.current`, the slot `Advance()` is about to process/has just processed, instead of a slot roughly `wheelDuration` in the future. (By contrast, when `max` *is* an exact multiple of `min`, the same derivation yields `tick == current-1`, i.e. correctly the furthest future slot before a full wheel revolution — so the bug is specific to the non-divisible case.)

The firewall wires this wheel directly to attacker-influenced traffic: `Firewall` builds its `TimerWheel`/`LockingTimerWheel` from `min`/`max` derived from the three configured timeouts (TCP/UDP/default), and `Firewall.addConn` calls `TimerWheel.Add(fp, timeout)` using exactly one of those configured durations depending on the packet's protocol [3](#0-2) . Whichever protocol carries the largest of the three configured timeouts will hit `timeout == wheelDuration` on every new connection of that protocol. If the operator's configured `tcp_timeout`/`udp_timeout`/`default_timeout` values are not related by an exact integer ratio (a very common case in practice, e.g. `10m`/`4m`/`12m`), any first packet of the protocol carrying the maximum timeout causes its conntrack entry to be scheduled into the currently-processed tick.

No existing check catches this: `Add` unconditionally trusts `findWheel`'s output, and `Advance`/`Purge`/`evict` have no validation that a newly-inserted item isn't already due.

### Impact Explanation
This is the “early eviction” direction described in the question: an attacker sending ordinary permitted traffic of the protocol whose configured timeout equals the wheel's `max` can cause that connection's conntrack/firewall state to be evicted almost immediately after creation (on the very next `Advance()` tick) instead of after the intended multi-minute timeout. This is a minor availability/self-inflicted churn issue — legitimate flows may be treated as new/dropped prematurely, causing extra conntrack re-adds or transient drops. I was **not** able to substantiate the inverse ("late eviction"/firewall-bypass) direction: the minimum possible offset from `tw.current` produced by `findWheel` is always `current+2` (never `current` or `current+1`), so short-timeout boundary values do not produce an under-shoot/bypass. Only the maximum-boundary, non-divisible-ratio case produces the `tick == current` collapse, and it always manifests as premature eviction, not a security bypass extending trust windows.

### Likelihood Explanation
Trigger requires only that (a) the operator's three configured timeouts are not related by an exact integer ratio, and (b) an attacker sends traffic of the protocol carrying the largest configured timeout, which happens on essentially every first packet of that protocol — no special privileges or config control needed beyond normal permitted traffic. This is deterministic (not probabilistic) given such a configuration, and repeatable on every new connection of the max-timeout protocol.

### Recommendation
In `NewTimerWheel`, compute `wLen` using a ceiling division that accounts for non-divisible ratios, e.g. `wLen := int((max+min-1)/min) + 2`, and/or in `findWheel` explicitly guard against the collapsed case by ensuring the final `tick` is never equal to `tw.current` (e.g. clamp/assert `tick != tw.current` and bump by one more slot if so), rather than relying on the arithmetic identity holding only for divisible ratios.

### Proof of Concept
Unit/invariant test in `timeout_test.go`:
```go
func TestFindWheel_MaxBoundaryNonDivisible(t *testing.T) {
    min := 4 * time.Minute
    max := 10 * time.Minute // 10/4 not an integer ratio
    tw := NewTimerWheel[int](min, max)
    tw.current = 3 // arbitrary current tick

    tick := tw.findWheel(max)
    assert.NotEqual(t, tw.current, tick, "item scheduled at max timeout must not land on the current tick")
}
```
Run with `current` swept across `0..wheelLen-1` and `(min,max)` pairs with non-integer `max/min` ratios (e.g. `4m/10m`, `3m/10m`, `7m/20m`); assert `tick != tw.current` in all cases. Expect the test to fail against the current implementation, confirming `findWheel` returns `tw.current` whenever `max % min != 0`.

### Citations

**File:** timeout.go (L80-90)
```go
	// Round down and add 2 so we can have the smallest # of ticks in the wheel and still account for a full
	// max duration, even if our current tick is at the maximum position and the next item to be added is at maximum
	// timeout
	wLen := int((max / min) + 2)

	tw := TimerWheel[T]{
		wheelLen:      wLen,
		wheel:         make([]*TimeoutList[T], wLen),
		tickDuration:  min,
		wheelDuration: max,
		expired:       &TimeoutList[T]{},
```

**File:** timeout.go (L164-184)
```go
func (tw *TimerWheel[T]) findWheel(timeout time.Duration) (i int) {
	if timeout < tw.tickDuration {
		// Can't track anything below the set resolution
		timeout = tw.tickDuration
	} else if timeout > tw.wheelDuration {
		// We aren't handling timeouts greater than the wheels duration
		timeout = tw.wheelDuration
	}

	// Find the next highest, rounding up
	tick := int(((timeout - 1) / tw.tickDuration) + 1)

	// Add another tick since the current tick may almost be over then map it to the wheel from our
	// current position
	tick += tw.current + 1
	if tick >= tw.wheelLen {
		tick -= tw.wheelLen
	}

	return tick
}
```

**File:** firewall.go (L1-1)
```go
package nebula
```
