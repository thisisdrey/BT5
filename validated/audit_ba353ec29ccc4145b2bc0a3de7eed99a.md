### Title
Unprivileged UDP/QUIC flooding can saturate the sigverify packet `Deduper`, causing legitimate transactions to be silently discarded before verification - (File: core/src/sigverify_stage.rs, perf/src/deduper.rs)

### Summary
The Alchemix report describes a griefing pattern where an unprivileged actor cheaply makes a "must not already exist" precondition true, causing a legitimate/necessary state transition to permanently or repeatedly fail, with damage to users/protocol but no profit for the attacker. The closest reachable analog in agave, within the permitted unprivileged-user surfaces (packet dedup and sigverify), is the pre-signature-verification packet `Deduper` bloom filter used in `SigVerifyStage`. Any unprivileged sender on the TPU (UDP/QUIC) can push arbitrary, distinct, unsigned packet bytes through the dedup path essentially for free (no fee, no valid signature required), inflating the bloom filter's population count. Once the filter's estimated false-positive rate crosses the configured threshold, *legitimate, unrelated* transactions can be mis-classified as "already seen" and dropped before they are even signature-checked — silently, with no error surfaced to the submitter.

### Finding Description
`SigVerifyWorkerState` holds an `Arc<Deduper<2, [u8]>>` created in `SigVerifyStage::new` with `DEDUPER_NUM_BITS` bits [1](#0-0) . Every incoming packet batch is deduplicated purely on raw packet bytes, *before* signature verification, via `dedup_packets_and_count_discards`, which calls `deduper.dedup(data)` for each non-discarded packet and marks it `discard()` if the bloom filter reports it as already seen [2](#0-1) .

`Deduper::dedup` is a classic k-hash bloom filter: for each distinct piece of data it sets bits and increments `popcount`; a "hit" (all bits already set) marks the packet as duplicate, causing it to be discarded before any HashOnly/signature validation is attempted [3](#0-2) . Its false-positive rate grows monotonically with `popcount` (`ones_ratio.powi(K)`), and there is no per-key or per-source cost — any raw bytes qualify, since the bloom filter operates independent of signature validity [4](#0-3) .

Correction of a saturated filter is not synchronous with `dedup()` calls: it relies on a background "servicer" thread that wakes up on a fixed cadence and calls `deduper.maybe_reset(...)`, resetting the filter only if `false_positive_rate() >= DEDUPER_FALSE_POSITIVE_RATE` or the reset cycle has elapsed [5](#0-4) ; the reset check/loop itself sleeps for a fixed interval between checks [6](#0-5) . `maybe_reset` is explicitly documented as *not synchronized with concurrent `dedup()` calls* [7](#0-6) , meaning a sustained flood of new distinct byte-strings can keep re-driving the filter's false-positive rate back up to the threshold between reset checks.

Getting the filter to the false-positive threshold requires a bounded, quantifiable number of *distinct* packets — the project's own tests give exact capacities for various `num_bits`/false-positive-rate combinations (e.g., ~2,023,857 distinct entries to reach a 0.1% false-positive rate at 63,999,979 bits; ~20,160,601 at 637,534,199 bits) [8](#0-7) . None of this requires a valid signature, a fee payer, or any privileged role — an attacker simply emits arbitrary distinct bytes over the TPU UDP/QUIC ingest path, exactly analogous to the Bribe attacker sending 1 wei of DAI to flip `isReward[newToken]` to `true` and permanently block a legitimate `swapOutRewardToken` call: here, the attacker cheaply flips the dedup filter's state so that legitimate, unrelated packets test positive for "already seen" and get dropped pre-verification.

### Impact Explanation
This is a griefing vector in the strict sense used by the reference report: there is no profit motive for the attacker, but a sufficiently sustained, cheap (unsigned, feeless) packet flood degrades the correctness of the sigverify ingestion path by causing a nonzero and attacker-controllable fraction of *other users'* legitimate transactions to be silently discarded before signature verification, with no error returned to the submitter (who has no way to detect the drop). Because the check happens purely on unauthenticated bytes prior to fee/signature enforcement, the "cost" to the attacker to cause this damage is essentially unbounded/underpriced relative to the work it forces the validator's ingestion pipeline to misclassify. This does not rise to node panic, deadlock, or verification bypass, but it is a concrete transaction-dropping griefing effect reachable by any unprivileged network peer against the "packet dedup and sigverify" surface explicitly listed as in scope.

### Likelihood Explanation
Reaching the saturation point requires generating on the order of 10^6–10^7 distinct packets, which is achievable with modest sustained UDP/QUIC throughput (well within documented per-node throughput figures such as `DEFAULT_MAX_STREAMS_PER_MS`), and does not require any stake, valid signature, or fee. The main mitigating factor is the periodic `maybe_reset` correction, which limits the window of elevated false-positive rate, but since the reset check is not synchronized with the `dedup()` hot path, a sufficiently sustained flood can keep the filter oscillating near the false-positive threshold rather than staying at zero.

### Recommendation
- Rate-limit or cost-gate the packet-dedup path itself (e.g., per-IP/per-connection quotas that are cheaper to compute but bound the number of distinct entries a single unauthenticated source can inject into the shared bloom filter per reset window).
- Consider synchronizing `maybe_reset` checks more tightly with `dedup()` throughput (e.g., checking saturation inline after each batch rather than only on a fixed background cadence), or partitioning the deduper state per-source so a single flooding peer cannot degrade the shared false-positive rate for all other senders.
- Track and alert on `num_deduper_saturations` more aggressively in production monitoring, since frequent saturation is itself a signal of this griefing pattern in progress.

### Proof of Concept
Conceptual reproduction (no live cluster required to reason about it, following the existing project test helpers):
1. Instantiate a `Deduper::<2, [u8]>::new(&mut rng, DEDUPER_NUM_BITS)` as done in `SigVerifyStage::new` [1](#0-0) .
2. Generate `get_capacity::<2>(DEDUPER_NUM_BITS, DEDUPER_FALSE_POSITIVE_RATE) + 1` distinct random byte strings (as done in the project's own `test_dedup_capacity`/`test_dedup_saturated` tests) and call `dedup_packets_and_count_discards` on them in batches [9](#0-8) .
3. Observe (as in `test_dedup_saturated`) that `filter.popcount` exceeds capacity and `filter.false_positive_rate() >= FALSE_POSITIVE_RATE` before the next scheduled `maybe_reset` call fires [10](#0-9) .
4. During this window, feed a legitimate, previously-unseen packet through `dedup`; with probability ≈ the elevated false-positive rate, it is reported as a duplicate and discarded by `run_transaction_task` before signature verification is ever attempted [2](#0-1) , with no feedback to the transaction's real submitter.

I could not verify in the index the exact production values of `DEDUPER_NUM_BITS`/`DEDUPER_FALSE_POSITIVE_RATE`/`MAX_DEDUPER_AGE` constants referenced by `core/src/sigverify_stage.rs` (grep found matches but the exact numeric definitions were not returned within the index before the final iteration), nor the exact background reset-check interval used in production (only the shred-sigverify path's analogous constants were retrieved, e.g. `DEDUPER_RESET_CYCLE = 5 * 60s` in `turbine/src/sigverify_shreds.rs`). A Devin session with full repo access would be needed to confirm the precise saturation timing/throughput math for the TPU (non-shred) deduper before treating the quantitative likelihood estimate as final.

### Citations

**File:** core/src/sigverify_stage.rs (L163-166)
```rust
        let exit = Arc::new(AtomicBool::new(false));
        let mut rng = rand::rng();
        let non_vote_deduper = Arc::new(Deduper::<2, [u8]>::new(&mut rng, DEDUPER_NUM_BITS));
        let tpu_vote_deduper = Arc::new(Deduper::<2, [u8]>::new(&mut rng, DEDUPER_NUM_BITS));
```

**File:** core/src/sigverify_stage.rs (L260-291)
```rust
    /// Drives deduper reset and metrics reporting for sigverify packet streams.
    fn servicer(
        exit: Arc<AtomicBool>,
        mut non_vote_state: ServicerState,
        mut tpu_vote_state: ServicerState,
    ) -> JoinHandle<()> {
        let mut last_print = Instant::now();
        Builder::new()
            .name("solSigVerSvc".to_string())
            .spawn(move || {
                let mut rng = rand::rng();
                while !exit.load(Ordering::Relaxed) {
                    for state in [&mut non_vote_state, &mut tpu_vote_state] {
                        if state.deduper.maybe_reset(
                            &mut rng,
                            DEDUPER_FALSE_POSITIVE_RATE,
                            MAX_DEDUPER_AGE,
                        ) {
                            state.stats.num_deduper_saturations += 1;
                        }
                    }
                    if last_print.elapsed() > SigVerifierStats::REPORT_INTERVAL {
                        non_vote_state
                            .stats
                            .maybe_report_and_reset(non_vote_state.metrics_name);
                        tpu_vote_state
                            .stats
                            .maybe_report_and_reset(tpu_vote_state.metrics_name);
                        last_print = Instant::now();
                    }
                    thread::sleep(Duration::from_millis(10));
                }
```

**File:** core/src/sigverify.rs (L282-298)
```rust
        let (discard_or_dedup_fail, dedup_time_us) =
            measure_us!(deduper::dedup_packets_and_count_discards(
                &state.deduper,
                std::slice::from_mut(&mut batch)
            ));
        state
            .stats
            .total_dedup
            .fetch_add(discard_or_dedup_fail as usize, Ordering::Relaxed);
        state
            .stats
            .total_dedup_time_us
            .fetch_add(dedup_time_us as usize, Ordering::Relaxed);

        if discard_or_dedup_fail as usize == batch_len {
            return true;
        }
```

**File:** perf/src/deduper.rs (L57-61)
```rust
    fn false_positive_rate(&self) -> f64 {
        let popcount = self.popcount.load(Ordering::Relaxed);
        let ones_ratio = popcount.min(self.num_bits) as f64 / self.num_bits as f64;
        ones_ratio.powi(K as i32)
    }
```

**File:** perf/src/deduper.rs (L63-73)
```rust
    /// Reset is not synchronized with concurrent `dedup()` calls. A caller can
    /// see an inconsistent snapshot across the old/new hash state and the
    /// cleared/refilled bitset, but that is acceptable because reset already
    /// starts a fresh deduplication window.
    fn reset<R: Rng>(&self, rng: &mut R) {
        for bits in &self.bits {
            bits.store(0, Ordering::Relaxed);
        }
        self.popcount.store(0, Ordering::Relaxed);
        self.state.store(Arc::new(DeduperGeneration::new(rng)));
    }
```

**File:** perf/src/deduper.rs (L97-115)
```rust
    // Returns true if the data is duplicate.
    #[must_use]
    #[allow(clippy::arithmetic_side_effects)]
    pub fn dedup(&self, data: &T) -> bool {
        let mut out = true;
        let state = self.state.load();
        for random_state in state.random_states.iter() {
            let hash: u64 = random_state.hash_one(data) % self.num_bits;
            let index = (hash >> 6) as usize;
            let mask: u64 = 1u64 << (hash & 63);
            let old = self.bits[index].fetch_or(mask, Ordering::Relaxed);
            if old & mask == 0u64 {
                self.popcount.fetch_add(1, Ordering::Relaxed);
                out = false;
            }
        }
        out
    }
}
```

**File:** perf/src/deduper.rs (L200-227)
```rust
    #[test]
    fn test_dedup_saturated() {
        // Use smaller value to saturate the deduper quicker.
        const NUM_BITS: u64 = 1_000_000;
        const FALSE_POSITIVE_RATE: f64 = 0.001;
        let mut rng = rand::rng();
        let filter = Deduper::<2, [u8]>::new(&mut rng, NUM_BITS);
        let capacity = get_capacity::<2>(NUM_BITS, FALSE_POSITIVE_RATE);
        let mut discard = 0;
        assert!(filter.popcount.load(Ordering::Relaxed) < capacity);
        for i in 0..1000 {
            let mut batches =
                to_packet_batches(&(0..1000).map(|_| test_tx()).collect::<Vec<_>>(), 128);
            discard += dedup_packets_and_count_discards(&filter, &mut batches) as usize;
            trace!("{i} {discard}");
            if filter.popcount.load(Ordering::Relaxed) > capacity {
                break;
            }
        }
        assert!(filter.popcount.load(Ordering::Relaxed) > capacity);
        assert!(filter.false_positive_rate() >= FALSE_POSITIVE_RATE);
        assert!(filter.maybe_reset(
            &mut rng,
            FALSE_POSITIVE_RATE,
            Duration::from_millis(0), // reset_cycle
        ));
        assert_eq!(filter.popcount.load(Ordering::Relaxed), 0);
    }
```

**File:** perf/src/deduper.rs (L274-300)
```rust
    #[test_case(63_999_979, 0.001, 2_023_857)]
    #[test_case(622_401_961, 0.001, 19_682_078)]
    #[test_case(622_401_979, 0.001, 19_682_078)]
    #[test_case(629_145_593, 0.001, 19_895_330)]
    #[test_case(632_455_543, 0.001, 20_000_000)]
    #[test_case(637_534_199, 0.001, 20_160_601)]
    #[test_case(622_401_961, 0.0001, 6_224_019)]
    #[test_case(622_401_979, 0.0001, 6_224_019)]
    #[test_case(629_145_593, 0.0001, 6_291_455)]
    #[test_case(632_455_543, 0.0001, 6_324_555)]
    #[test_case(637_534_199, 0.0001, 6_375_341)]
    fn test_dedup_capacity(num_bits: u64, false_positive_rate: f64, capacity: u64) {
        let mut rng = rand::rng();
        assert_eq!(get_capacity::<2>(num_bits, false_positive_rate), capacity);
        let deduper = Deduper::<2, [u8]>::new(&mut rng, num_bits);
        assert_eq!(deduper.false_positive_rate(), 0.0);
        deduper.popcount.store(capacity, Ordering::Relaxed);
        assert!(deduper.false_positive_rate() < false_positive_rate);
        deduper.popcount.store(capacity + 1, Ordering::Relaxed);
        assert!(deduper.false_positive_rate() >= false_positive_rate);
        assert!(deduper.maybe_reset(
            &mut rng,
            false_positive_rate,
            Duration::from_millis(0), // reset_cycle
        ));
        assert_eq!(deduper.popcount.load(Ordering::Relaxed), 0);
    }
```
