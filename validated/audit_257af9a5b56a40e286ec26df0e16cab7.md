### No vulnerability found for this question.

**Reasoning:** The `Deduper` is implemented as a fixed-size bit array allocated once at construction, not an unboundedly growing structure. Its size is `num_bits.checked_add(63) / 64` computed at construction time and stored as a `Vec<AtomicU64>` that is never resized during `dedup()` calls [1](#0-0) . The `dedup()` method only sets bits within this fixed bitset via hashing modulo `num_bits`; it cannot grow the structure regardless of packet volume or entropy [2](#0-1) .

Saturation (rising false-positive rate under high-entropy/near-duplicate traffic) is explicitly anticipated and bounded by `maybe_reset()`, which clears the fixed bitset and rotates hash seeds whenever the false-positive rate exceeds a configured threshold or the reset cycle elapses [3](#0-2) . In `SigVerifyStage`, a dedicated servicer thread polls `maybe_reset` every 10ms with fixed constants `DEDUPER_NUM_BITS`, `DEDUPER_FALSE_POSITIVE_RATE`, and `MAX_DEDUPER_AGE` [4](#0-3) [5](#0-4) . The existing unit test `test_dedup_saturated` confirms that once popcount exceeds the capacity threshold, `maybe_reset` returns true and resets popcount to 0, bounding both memory and false-positive rate [6](#0-5) .

Therefore, the premise that the bloom filter "grows" or that memory/CPU spent on bookkeeping grows unboundedly is factually incorrect — memory is fixed at construction and false-positive degradation is self-correcting via periodic reset, not unbounded. The worst-case effect of the described attack is a temporarily elevated false-positive rate (more duplicates passing through) until the next reset window (at most `MAX_DEDUPER_AGE` = 2 seconds, or sooner via saturation check), which is a known, accepted design tradeoff rather than a memory/CPU DoS.

### Citations

**File:** perf/src/deduper.rs (L44-55)
```rust
    pub fn new<R: Rng>(rng: &mut R, num_bits: u64) -> Self {
        let size = num_bits.checked_add(63).unwrap() / 64;
        let size = usize::try_from(size).unwrap();
        Self {
            num_bits,
            state: ArcSwap::from_pointee(DeduperGeneration::new(rng)),
            bits: repeat_with(AtomicU64::default).take(size).collect(),
            popcount: AtomicU64::default(),
            reset_guard: Mutex::default(),
            _phantom: PhantomData::<T>,
        }
    }
```

**File:** perf/src/deduper.rs (L82-95)
```rust
    pub fn maybe_reset<R: Rng>(
        &self,
        rng: &mut R,
        false_positive_rate: f64,
        reset_cycle: Duration,
    ) -> bool {
        assert!(0.0 < false_positive_rate && false_positive_rate < 1.0);
        let _reset_guard = self.reset_guard.lock().unwrap();
        let saturated = self.false_positive_rate() >= false_positive_rate;
        if saturated || self.state.load().started_at.elapsed() >= reset_cycle {
            self.reset(rng);
        }
        saturated
    }
```

**File:** perf/src/deduper.rs (L100-114)
```rust
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

**File:** core/src/sigverify_stage.rs (L38-40)
```rust
const DEDUPER_NUM_BITS: u64 = 63_999_979;
const MAX_DEDUPER_AGE: Duration = Duration::from_secs(2);
const DEDUPER_FALSE_POSITIVE_RATE: f64 = 0.001;
```

**File:** core/src/sigverify_stage.rs (L271-290)
```rust
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
```
