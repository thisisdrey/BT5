This finding does not hold up under scrutiny of the actual code and existing mitigations.

**Deduper cost is O(1) per packet, not superlinear.** The `Deduper` in `perf/src/deduper.rs` is a fixed-size Bloom-filter-style bit-set (`Vec<AtomicU64>`) allocated once at `Deduper::new()` with `DEDUPER_NUM_BITS = 63_999_979` (see `core/src/sigverify_stage.rs:38`) and never grows.`Deduper::dedup` does exactly K=2 hash computations and 2 atomic `fetch_or` operations per packet, regardless of whether the packet is unique or duplicate — this is standard constant-time Bloom-filter behavior, not something that scales with "packet distinctness." The bit array itself never resizes; it is fixed at construction (`perf/src/deduper.rs:44-55`) and only cleared via `maybe_reset` on a time/false-positive-rate trigger (`perf/src/deduper.rs:82-95`), which is bounded, periodic housekeeping, not attacker-triggered growth.

**Sigverify work per packet is bounded and is the intended design, not a bypassable guard.** `ed25519_verify_serial`/`ed25519_verify` (`perf/src/sigverify.rs:108-133`) perform one fixed-cost signature-view parse plus signature verification per non-discarded packet. This is inherent to Solana's fee model: since fees can only be attributed after verifying the transaction's signer, there is no way to fee-gate before verification — this is a known, accepted architectural property across all Solana validator versions, not a defect introduced in this codebase.

**Upstream QUIC ingress already bounds attacker-controlled packet volume before it reaches sigverify.** Unstaked/unauthenticated senders are throttled well before packets reach `SigVerifyStage`:
- Per-IP and global connection rate limiting in `streamer/src/nonblocking/quic.rs:270-370` (`ConnectionRateLimiter`, `overall_connection_rate_limiter`).
- Per-connection stream throttling capping unstaked traffic to `MAX_UNSTAKED_TPS = 200` in `streamer/src/nonblocking/stream_throttle.rs:17,233-271`.
- Configurable caps on max connections/streams (`DEFAULT_MAX_UNSTAKED_CONNECTIONS`, `DEFAULT_MAX_STREAMS_PER_MS`, etc. in `streamer/src/quic.rs:41-56`).

These mechanisms directly bound the packet rate an unstaked client can sustain, which in turn bounds total sigverify-stage work (`total_verify_time_us`, `total_dedup_time_us` in `core/src/sigverify.rs:42-55`) to a proportional, configured ceiling — exactly the invariant the question claims is violated.

Since (1) dedup cost is provably O(1)/packet with a fixed-size bit-set that cannot grow, (2) sigverify cost is a fixed per-packet operation whose necessity before fee attribution is an accepted, unavoidable design property (explicitly out of scope as "best-practice"/inherent design, not a bug), and (3) QUIC-layer rate limiting already caps unstaked packet ingress rate, there is no superlinear or unbounded resource-exhaustion path here. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4) [6](#0-5) [7](#0-6) [8](#0-7) 

#No vulnerability found for this question.

### Citations

**File:** perf/src/deduper.rs (L43-55)
```rust
impl<const K: usize, T: ?Sized + Hash> Deduper<K, T> {
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

**File:** perf/src/sigverify.rs (L108-133)
```rust
pub fn ed25519_verify(
    thread_pool: &rayon::ThreadPool,
    batches: &mut [PacketBatch],
    reject_non_vote: bool,
    packet_count: usize,
    enable_tx_v1: bool,
) {
    debug!("CPU ECDSA for {packet_count}");
    thread_pool.install(|| {
        batches.par_iter_mut().flatten().for_each(|mut packet| {
            if !packet.meta().discard()
                && !verify_packet(&mut packet, reject_non_vote, enable_tx_v1)
            {
                packet.meta_mut().set_discard(true);
            }
        });
    });
}

pub fn ed25519_verify_serial(batch: &mut PacketBatch, reject_non_vote: bool, enable_tx_v1: bool) {
    for mut packet in batch.iter_mut() {
        if !packet.meta().discard() && !verify_packet(&mut packet, reject_non_vote, enable_tx_v1) {
            packet.meta_mut().set_discard(true);
        }
    }
}
```

**File:** core/src/sigverify_stage.rs (L38-40)
```rust
const DEDUPER_NUM_BITS: u64 = 63_999_979;
const MAX_DEDUPER_AGE: Duration = Duration::from_secs(2);
const DEDUPER_FALSE_POSITIVE_RATE: f64 = 0.001;
```

**File:** streamer/src/nonblocking/quic.rs (L346-369)
```rust
            // check overall connection request rate limiter
            if overall_connection_rate_limiter.current_tokens() == 0 {
                stats
                    .connection_rate_limited_across_all
                    .fetch_add(1, Ordering::Relaxed);
                debug!(
                    "Ignoring incoming connection from {} due to overall rate limit.",
                    incoming.remote_address()
                );
                incoming.ignore();
                continue;
            }
            // then perform per IpAddr rate limiting
            if !rate_limiter.is_allowed(&incoming.remote_address().ip()) {
                stats
                    .connection_rate_limited_per_ipaddr
                    .fetch_add(1, Ordering::Relaxed);
                debug!(
                    "Ignoring incoming connection from {} due to per-IP rate limiting.",
                    incoming.remote_address()
                );
                incoming.ignore();
                continue;
            }
```

**File:** streamer/src/nonblocking/stream_throttle.rs (L16-17)
```rust
/// Max TPS allowed for unstaked connection
const MAX_UNSTAKED_TPS: u64 = 200;
```

**File:** streamer/src/nonblocking/stream_throttle.rs (L233-271)
```rust
pub(crate) async fn throttle_stream(
    stats: &StreamerStats,
    peer_type: ConnectionPeerType,
    remote_addr: std::net::SocketAddr,
    stream_counter: &Arc<ConnectionStreamCounter>,
    max_streams_per_throttling_interval: u64,
) {
    let throttle_interval_start = stream_counter.reset_throttling_params_if_needed();
    let streams_read_in_throttle_interval = stream_counter.stream_count.load(Ordering::Relaxed);
    if streams_read_in_throttle_interval >= max_streams_per_throttling_interval {
        // The peer is sending faster than we're willing to read. Sleep for what's
        // left of this read interval so the peer backs off.
        let throttle_duration =
            STREAM_THROTTLING_INTERVAL.saturating_sub(throttle_interval_start.elapsed());

        if !throttle_duration.is_zero() {
            debug!(
                "Throttling stream from {remote_addr:?}, peer type: {peer_type:?}, \
                 max_streams_per_interval: {max_streams_per_throttling_interval}, \
                 read_interval_streams: {streams_read_in_throttle_interval} throttle_duration: \
                 {throttle_duration:?}"
            );
            stats.throttled_streams.fetch_add(1, Ordering::Relaxed);
            match peer_type {
                ConnectionPeerType::Unstaked => {
                    stats
                        .throttled_unstaked_streams
                        .fetch_add(1, Ordering::Relaxed);
                }
                ConnectionPeerType::Staked(_) => {
                    stats
                        .throttled_staked_streams
                        .fetch_add(1, Ordering::Relaxed);
                }
            }
            sleep(throttle_duration).await;
        }
    }
}
```

**File:** streamer/src/quic.rs (L41-56)
```rust
pub const DEFAULT_MAX_QUIC_CONNECTIONS_PER_UNSTAKED_PEER: usize = 8;

// allow multiple connections per ID for geo-distributed forwarders
pub const DEFAULT_MAX_QUIC_CONNECTIONS_PER_STAKED_PEER: usize = 16;

pub const DEFAULT_MAX_STAKED_CONNECTIONS: usize = 2000;

pub const DEFAULT_MAX_UNSTAKED_CONNECTIONS: usize = 2000;

/// Limit to 500K PPS
pub const DEFAULT_MAX_STREAMS_PER_MS: u64 = 500;

/// The new connections per minute from a particular IP address.
/// Heuristically set to the default maximum concurrent connections
/// per IP address. Might be adjusted later.
pub const DEFAULT_MAX_CONNECTIONS_PER_IPADDR_PER_MINUTE: u64 = 8;
```
