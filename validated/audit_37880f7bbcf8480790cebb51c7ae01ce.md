### Title
`DynamicPacketToProcessThreshold::max_packets` has no floor guard and permanently latches at 0 after 50 consecutive `Decrease` updates - ([File: core/src/repair/packet_threshold.rs])

### Summary
`DynamicPacketToProcessThreshold::update()` repeatedly applies `PacketThresholdUpdate::Decrease` (integer `current * 90 / 100`) whenever `total_packets >= max_packets` and `compute_time > 1s`, with no lower bound check. Starting from `DEFAULT_MAX_PACKETS = 1024`, exactly 50 consecutive `Decrease` events drive `max_packets` to `0`, after which the value becomes mathematically un-recoverable: both `Increase` (`0 * 100 / 90 = 0`) and `Decrease` (`0 * 90 / 100 = 0`) leave it at `0` forever.

### Finding Description
`DynamicPacketToProcessThreshold::update` at [1](#0-0)  only mutates `max_packets` under the guard `total_packets >= self.max_packets`. Because `total_packets: usize` is always `>= 0`, once `max_packets` reaches `0` this guard is unconditionally true for every future call, so `update()` never becomes a no-op. The direction of change (`Increase` vs `Decrease`) is selected purely by whether `compute_time > Self::TIME_THRESHOLD` (1s), as seen in [2](#0-1) .

The actual arithmetic is defined in `PacketThresholdUpdate::calculate`: [3](#0-2) . For `current = 0`:
- `Increase`: `0.saturating_mul(100).saturating_div(90) = 0`
- `Decrease`: `0.saturating_mul(90).saturating_div(100) = 0`

Both branches map `0 -> 0`, so once the threshold hits zero it can never recover, regardless of how many subsequent `Increase` events occur. Tracing the truncating sequence `x_{n+1} = floor(x_n * 90 / 100)` starting at `x_0 = 1024`, the value reaches `0` after exactly 50 consecutive `Decrease` steps (1024→921→828→745→670→603→542→487→438→394→354→318→286→257→231→207→186→167→150→135→121→108→97→87→78→70→63→56→50→45→40→36→32→28→25→22→19→17→15→13→11→9→8→7→6→5→4→3→2→1→0).

Once `max_packets == 0`, `should_drop` at [4](#0-3)  returns `true` for any `total: usize` (including `0`), meaning the associated packet-processing path permanently treats every batch as "over threshold" and never lets the adaptive threshold recover, since `Increase` from `0` is a fixed point too.

### Impact Explanation
This structure is used by the repair subsystem, specifically `ancestor_hashes_service.rs` and `block_id_repair_service.rs` [5](#0-4) [6](#0-5) , to adaptively decide whether incoming repair-related packet batches exceed a processing budget. Driving `max_packets` to `0` and latching it there means `should_drop` will unconditionally report "drop" thereafter, which can permanently stall processing on the code path guarded by this threshold — a self-inflicted, non-recoverable degradation of that repair-processing pipeline until the validator process restarts (which resets `Default::default()`). This is a logic/availability defect: an integer-truncation floor bug with no guard, matching a DoS/availability-impact category rather than a consensus-safety issue.

### Likelihood Explanation
The bug is deterministic and requires no network conditions beyond feeding the local counter 50 consecutive `(total_packets >= max_packets, compute_time > 1s)` observations — entirely reproducible in an isolated unit test with no mocks, no store mutation, and no privileged access, since `update()` and `should_drop()` are public safe functions operating purely on local state [7](#0-6) . In production this would require sustained conditions where packet volume exceeds the shrinking threshold while processing consistently exceeds 1 second — plausible for a sustained flood, though the exact rate of `update()` invocations from the repair path is driven by internal loop cadence, not directly by attacker.

### Recommendation
Add a floor guard, e.g. `const MIN_MAX_PACKETS: usize = 1;` and clamp `self.max_packets = threshold_update.calculate(self.max_packets).max(Self::MIN_MAX_PACKETS);`, or equivalently skip the decrease when `self.max_packets <= MIN_MAX_PACKETS`, so that `max_packets` can never fall to `0` and the `Increase` path always has a non-zero base to recover from.

### Proof of Concept
```rust
// core/src/repair/packet_threshold.rs (add to #[cfg(test)] mod test)
#[test]
fn test_threshold_latches_at_zero_with_no_floor() {
    let mut threshold = DynamicPacketToProcessThreshold::default();
    assert_eq!(threshold.max_packets, DynamicPacketToProcessThreshold::DEFAULT_MAX_PACKETS);

    let slow = Duration::from_secs(2); // > TIME_THRESHOLD triggers Decrease
    let mut steps = 0usize;
    // Drive max_packets to 0 purely via Decrease; total_packets kept huge so guard is always true.
    while threshold.max_packets > 0 {
        threshold.update(usize::MAX, slow);
        steps += 1;
        assert!(steps <= 50, "expected to reach 0 within 50 decreases");
    }
    assert_eq!(steps, 50, "exactly 50 consecutive decreases drive 1024 -> 0");
    assert_eq!(threshold.max_packets, 0);

    // Now assert the non-recoverable invariant: no finite sequence of calls,
    // Increase or Decrease, can move max_packets off 0.
    let fast = Duration::from_millis(1); // triggers Increase
    for _ in 0..1000 {
        threshold.update(usize::MAX, fast); // Increase branch: 0*100/90 = 0
        threshold.update(usize::MAX, slow);  // Decrease branch: 0*90/100 = 0
        assert_eq!(threshold.max_packets, 0, "threshold permanently stuck at 0 - no floor guard");
    }

    // should_drop is now unconditionally true, even for an empty batch.
    assert!(threshold.should_drop(0));
}
```
Expected result on current code: the test passes, proving `max_packets` reaches `0` after exactly 50 `Decrease` calls and then remains `0` forever under any mix of `Increase`/`Decrease` inputs — confirming the missing floor guard. After applying the recommended clamp, the second loop's assertion `max_packets == 0` should fail (threshold would instead stay at `MIN_MAX_PACKETS` and recover via `Increase`), which is the expected regression-proof behavior for the fix.

### Citations

**File:** core/src/repair/packet_threshold.rs (L11-20)
```rust
    fn calculate(&self, current: usize) -> usize {
        match *self {
            PacketThresholdUpdate::Increase => {
                current.saturating_mul(100).saturating_div(Self::PERCENTAGE)
            }
            PacketThresholdUpdate::Decrease => {
                current.saturating_mul(Self::PERCENTAGE).saturating_div(100)
            }
        }
    }
```

**File:** core/src/repair/packet_threshold.rs (L36-53)
```rust
impl DynamicPacketToProcessThreshold {
    const DEFAULT_MAX_PACKETS: usize = 1024;
    const TIME_THRESHOLD: Duration = Duration::from_secs(1);

    pub fn update(&mut self, total_packets: usize, compute_time: Duration) {
        if total_packets >= self.max_packets {
            let threshold_update = if compute_time > Self::TIME_THRESHOLD {
                PacketThresholdUpdate::Decrease
            } else {
                PacketThresholdUpdate::Increase
            };
            self.max_packets = threshold_update.calculate(self.max_packets);
        }
    }

    pub fn should_drop(&self, total: usize) -> bool {
        total >= self.max_packets
    }
```

**File:** core/src/repair/ancestor_hashes_service.rs (L1-1)
```rust
use {
```

**File:** core/src/repair/block_id_repair_service.rs (L1-1)
```rust
//! Service responsible for fetching alternate versions of blocks through informed repair.
```
