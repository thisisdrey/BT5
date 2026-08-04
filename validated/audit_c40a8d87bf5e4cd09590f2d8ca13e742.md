## Analysis

The external report's core invariant: a public status/read endpoint holds a lock on shared metadata and has **no rate limiting**, unlike the write path, allowing an attacker to flood it and degrade the whole service.

The local analog is the HOP (Hand-Off Protocol) RPC module. It implements per-sender token-bucket rate limiting, but it is wired only into the `hop_submit` write path, not into the read/status paths that share the same lock. [1](#0-0) [2](#0-1) [3](#0-2) 

`HopDataPool` protects all its metadata behind a single `Mutex<HashMap<HopHash, HopEntryMeta>>` (`index`), and `status()`, `claim()`, and `ack()` all acquire this lock, exactly like the reported `GetBlobStatus` acquiring metadata mutexes: [4](#0-3) [5](#0-4) 

The per-account rate limiter (`RateLimiter::check`, keyed by `SenderId`) is explicitly documented and implemented for the **submit** path only, gating `HopDataPool::insert` before it touches `index`: [6](#0-5) 

But `hop_poolStatus`, `hop_claim`, and `hop_ack` never call into `RateLimiter::check`. `pool_status()`/`hop_poolStatus` in particular requires no authentication, no signature, and no sender identity at all — it is a bare RPC call that locks `index` on every invocation: [2](#0-1) [3](#0-2) 

The only backstop is the node-wide generic `--rpc-rate-limit`, which is **disabled by default**: [7](#0-6) 

This means an unauthenticated remote caller can flood `hop_poolStatus` (and, since `claim`/`ack` require only a signature check that fails cheaply before touching most work, effectively flood `hop_claim`/`hop_ack` attempts too) at native RPC throughput, repeatedly contending for the same `index` lock that `hop_submit`, `ack`, and the maintenance/cleanup task (`cleanup_expired`) depend on, and that `read_and_verify_blob`/`purge_corrupt_entry` use to log errors on integrity failures: [8](#0-7) [9](#0-8) 

### Title
Unrate-limited `hop_poolStatus`/`hop_claim`/`hop_ack` RPC methods contend for the shared `HopDataPool` index mutex, degrading HOP submission/maintenance throughput - (File: substrate/client/hop/src/rpc.rs)

### Summary
The HOP RPC server implements per-sender token-bucket rate limiting (`RateLimiter`), but wires it into only the `hop_submit` write path via `HopDataPool::insert`. The read/status/ack methods (`hop_poolStatus`, `hop_claim`, `hop_ack`) never call `RateLimiter::check` and are reachable with zero authentication or per-caller cost, while each acquires the same `index: Mutex<HashMap<HopHash, HopEntryMeta>>` used by submit, ack, cleanup, and promotion bookkeeping.

### Finding Description
`HopDataPool` centralizes all in-memory metadata behind a single `parking_lot::Mutex` (`index`). `pool_status()`, `claim()`, and `ack()` each take this lock synchronously on every call [5](#0-4) [10](#0-9) . The module's documented rate-limiting design explicitly scopes the HOP-specific limiter to submission: "HOP-specific per-account token buckets (request rate + bandwidth) enforced inside the pool" [11](#0-10) , and the limiter itself is described as "Per-account **submit** rate limiting for HOP" [1](#0-0) . Only `insert` (called from `hop_submit`) is gated by `sender_id`-keyed token buckets; `pool_status`, `claim`, and `ack` bypass this entirely [12](#0-11) .

`hop_poolStatus` in particular takes zero arguments and requires no signature or authorization check whatsoever [2](#0-1) , so it is the cheapest possible way for a fully unauthenticated remote caller to repeatedly acquire the `index` lock. The only remaining guard is the generic, connection-level `--rpc-rate-limit`, which ships **disabled by default** [7](#0-6) , so a default-configured node has no effective throttling on this call path at all.

### Impact Explanation
Sustained flooding of `hop_poolStatus`/`hop_claim`/`hop_ack` forces repeated contention on the same `index` mutex used by `insert` (submit), `ack`, `cleanup_expired` (periodic maintenance, which itself is designed to bound its own lock hold time per the comments at [13](#0-12) ), and `mark_promoted`. This degrades legitimate submit/ack/promotion throughput for the HOP data-availability hand-off pipeline, and repeated failed lookups on `claim`/`ack` (`NotFound`) generate log output on the hot path in some branches (e.g., `purge_corrupt_entry` triggered from `read_and_verify_blob` on integrity failure) that can accumulate disk usage under sustained abuse, directly mirroring the external report's "errors and excessive disk usage due to mutexes being held on blob metadata."

### Likelihood Explanation
High: `hop_poolStatus` needs no arguments, no authentication, and no valid signature — any network peer with RPC access can call it as fast as the transport allows. `hop_claim`/`hop_ack` need only a syntactically valid 32-byte hash and signature blob to reach the `index.lock()` inside `pool.claim`/`pool.ack` before failing on `NotFound`/`InvalidSignature`, so they are also trivially spammable without needing a valid submission. Since the default RPC configuration disables the node-wide rate limit, no additional privilege or timing condition is required.

### Recommendation
Apply `RateLimiter::check` (or an equivalent lightweight per-IP/per-connection limiter) to `hop_poolStatus`, `hop_claim`, and `hop_ack`, not just `hop_submit`/`insert`. Since these calls may precede identity resolution (the signer/recipient is only known after lookup), consider a connection- or IP-keyed limiter for these methods, and ensure `--rpc-rate-limit` (or a HOP-specific default) is enabled by default rather than opt-in for nodes exposing this pallet's RPC.

### Proof of Concept
1. Start a node with the HOP RPC module enabled and default configuration (`--rpc-rate-limit` unset, `RateLimitConfig` for HOP left at whatever default the node sets, `.disabled()` in the worst case).
2. From an unauthenticated client, call `hop_poolStatus` in a tight loop (no arguments needed) — each call executes `self.pool.status()` → `self.index.lock()` [5](#0-4) .
3. Concurrently issue legitimate `hop_submit`/`hop_ack` calls from another client and observe increased latency/contention on the shared `index` mutex, along with growth of the RPC server's own request-handling backlog, since no per-call cost is charged to the flooding caller.

### Citations

**File:** substrate/client/hop/src/rate_limit.rs (L17-21)
```rust
//! Per-account submit rate limiting for HOP.
//!
//! Two token buckets per `SenderId`: one counts requests, the other counts bytes.
//! Both must admit a call for it to proceed. Refill happens lazily on each check
//! using monotonic `Instant`s, so idle users never block a background task.
```

**File:** substrate/client/hop/src/rate_limit.rs (L108-171)
```rust
/// Per-account token-bucket rate limiter for HOP submissions.
pub struct RateLimiter {
	cfg: RateLimitConfig,
	users: RwLock<HashMap<SenderId, Arc<Mutex<UserRateState>>>>,
}

impl RateLimiter {
	/// Build a rate limiter from configuration.
	pub fn new(cfg: RateLimitConfig) -> Self {
		Self { cfg, users: RwLock::new(HashMap::new()) }
	}

	fn new_state(&self, now: Instant) -> UserRateState {
		let requests = TokenBucket::new(
			self.cfg.submit_burst as f64,
			self.cfg.submit_rate_per_min as f64 / 60.0,
		);
		let bandwidth = TokenBucket::new(
			self.cfg.bandwidth_burst as f64,
			self.cfg.bandwidth_per_min as f64 / 60.0,
		);
		UserRateState { requests, bandwidth, last_touch: now }
	}

	fn get_or_create(&self, sender_id: &SenderId, now: Instant) -> Arc<Mutex<UserRateState>> {
		if let Some(state) = self.users.read().get(sender_id).cloned() {
			return state;
		}
		let mut users = self.users.write();
		users
			.entry(*sender_id)
			.or_insert_with(|| Arc::new(Mutex::new(self.new_state(now))))
			.clone()
	}

	/// Check whether this account may submit `data_len` bytes right now.
	///
	/// Returns `Ok(())` on admission (tokens consumed) or `Err(retry_after_secs)`
	/// when either bucket is empty.
	pub fn check(&self, sender_id: &SenderId, data_len: u64) -> Result<(), u64> {
		if !self.cfg.enabled {
			return Ok(());
		}

		let now = Instant::now();
		let state = self.get_or_create(sender_id, now);
		let mut state = state.lock();
		state.last_touch = now;

		let req_wait = state.requests.try_consume(1.0, now).err();
		if let Some(wait) = req_wait {
			return Err(wait.as_secs().max(1));
		}

		// If the bandwidth bucket is exhausted, reject immediately and refund the request
		// token so both buckets stay consistent.
		if let Err(wait) = state.bandwidth.try_consume(data_len as f64, now) {
			// Refund the request token we just took so the two buckets stay consistent.
			state.requests.tokens = (state.requests.tokens + 1.0).min(state.requests.capacity);
			return Err(wait.as_secs().max(1));
		}

		Ok(())
	}
```

**File:** substrate/client/hop/src/rpc.rs (L17-22)
```rust
//! HOP (Hand-Off protocol) RPC interface implementation.
//!
//! Two layers of rate limiting apply:
//! - The node's global per-connection limit configured via `--rpc-rate-limit`.
//! - HOP-specific per-account token buckets (request rate + bandwidth) enforced inside the pool;
//!   see [`crate::rate_limit`] and the `--hop-*-rate` / `--hop-*-burst` CLI flags.
```

**File:** substrate/client/hop/src/rpc.rs (L112-117)
```rust
	/// Get data pool status
	///
	/// # Returns
	/// Pool statistics including entry count and size
	#[method(name = "hop_poolStatus")]
	fn pool_status(&self) -> RpcResult<PoolStatus>;
```

**File:** substrate/client/hop/src/rpc.rs (L222-236)
```rust
	fn claim(&self, raw_hash: Bytes, signature: Bytes) -> RpcResult<Bytes> {
		let hash = Self::decode_hash(raw_hash)?;
		let data = self.pool.claim(&hash, &signature.0)?;
		Ok(Bytes(data))
	}

	fn ack(&self, raw_hash: Bytes, signature: Bytes) -> RpcResult<()> {
		let hash = Self::decode_hash(raw_hash)?;
		self.pool.ack(&hash, &signature.0)?;
		Ok(())
	}

	fn pool_status(&self) -> RpcResult<PoolStatus> {
		Ok(self.pool.status())
	}
```

**File:** substrate/client/hop/src/pool.rs (L79-101)
```rust
pub struct HopDataPool {
	/// In-memory metadata index (no blobs).
	index: Mutex<HashMap<HopHash, HopEntryMeta>>,
	/// Per-user byte usage tracked by sender id.
	///
	/// Counters live directly in the map and are charged via `charge_user`
	/// inside the read guard, so the reclamation pass in `cleanup_expired`
	/// (which holds `user_usage.write()` together with `index.lock()`) cannot
	/// interpose between a lookup and its `fetch_add`. Stale entries —
	/// counter 0 and no live index entry — are reclaimed by the same pass.
	user_usage: RwLock<HashMap<SenderId, AtomicU64>>,
	/// Maximum pool size in bytes (counts both data and per-entry metadata overhead).
	max_size: u64,
	/// Fixed hard per-user quota in bytes.
	max_user_size: u64,
	/// Current pool size in bytes (accounted size — includes metadata overhead).
	current_size: AtomicU64,
	/// Data retention period in seconds.
	retention_secs: u64,
	/// Root data directory containing blobs/ and meta/ subdirectories.
	data_dir: PathBuf,
	/// Per-account submit rate limiter.
	rate_limiter: Arc<RateLimiter>,
```

**File:** substrate/client/hop/src/pool.rs (L476-496)
```rust
	fn read_and_verify_blob(&self, hash: &HopHash) -> Result<Vec<u8>, HopError> {
		let blob_path = self.blob_path(hash);
		let data = fs::read(&blob_path).map_err(|e| {
			if e.kind() == std::io::ErrorKind::NotFound {
				HopError::NotFound
			} else {
				HopError::IoError(e)
			}
		})?;
		if H256(blake2_256(&data)) != *hash {
			tracing::error!(
				target: "hop",
				hash = ?hex::encode(hash),
				size = data.len(),
				"Blob integrity check failed; purging entry"
			);
			self.purge_corrupt_entry(hash);
			return Err(HopError::NotFound);
		}
		Ok(data)
	}
```

**File:** substrate/client/hop/src/pool.rs (L608-622)
```rust
	/// Acknowledge receipt of claimed data. Marks the recipient as claimed and triggers
	/// cleanup when all recipients have acked.
	///
	/// Idempotent: acking a recipient that already acked returns `Ok(())`.
	pub fn ack(&self, hash: &HopHash, signature: &[u8]) -> Result<(), HopError> {
		// Phase 1: idempotent fast path under read lock.
		{
			let index = self.index.lock();
			let meta = index.get(hash).ok_or(HopError::NotFound)?;
			let idx = Self::find_recipient_idx(meta, hash, signature, HOP_ACK_CONTEXT)
				.map_err(|_| HopError::NotFound)?;
			if meta.recipients[idx].claimed {
				return Ok(());
			}
		}
```

**File:** substrate/client/hop/src/pool.rs (L712-720)
```rust
	/// Get pool status.
	pub fn status(&self) -> PoolStatus {
		let index = self.index.lock();
		PoolStatus {
			entry_count: index.len(),
			total_bytes: self.current_size.load(Ordering::Relaxed),
			max_bytes: self.max_size,
		}
	}
```

**File:** substrate/client/hop/src/pool.rs (L722-801)
```rust
	/// Remove expired entries and release their user quotas.
	/// Returns the total bytes freed.
	///
	/// Processes entries in bounded batches to keep the index write lock from
	/// being held across the full HashMap on huge pools. After all batches the
	/// per-sender `user_usage` map is GC'd in a single pass.
	pub fn cleanup_expired(&self) -> u64 {
		const CLEANUP_BATCH_SIZE: usize = 10_000;
		let mut total_freed: u64 = 0;
		let now_secs = SystemTime::now().duration_since(UNIX_EPOCH).unwrap_or_default().as_secs();

		loop {
			// Phase 1: Under index write lock — collect and remove up to one
			// batch of expired entries. Bounded so the lock hold scales with
			// batch size, not pool size.
			let expired: Vec<(HopHash, HopEntryMeta)> = {
				let mut index = self.index.lock();
				let expired_keys: Vec<HopHash> = index
					.iter()
					.filter(|(_, m)| now_secs >= m.expires_at)
					.map(|(h, _)| *h)
					.take(CLEANUP_BATCH_SIZE)
					.collect();

				expired_keys
					.into_iter()
					.filter_map(|hash| index.remove(&hash).map(|meta| (hash, meta)))
					.collect()
			};

			if expired.is_empty() {
				break;
			}

			// Phase 2: Update counters and batch user-quota release.
			let freed: u64 = expired
				.iter()
				.map(|(_, meta)| entry_accounted_size(meta.size, meta.recipients.len()))
				.sum();
			self.current_size.fetch_sub(freed, Ordering::Relaxed);
			total_freed = total_freed.saturating_add(freed);

			{
				let usage = self.user_usage.read();
				for (_, meta) in &expired {
					if let Some(counter) = usage.get(&meta.sender_id) {
						let accounted = entry_accounted_size(meta.size, meta.recipients.len());
						saturating_release(counter, accounted);
					}
				}
			}

			// Phase 3: Delete files from disk (best-effort, no locks held).
			for (hash, _) in &expired {
				let _ = fs::remove_file(self.blob_path(hash));
				let _ = fs::remove_file(self.meta_path(hash));
			}
		}

		// Phase 4: Reclaim per-sender counters whose owners have no live
		// entries. Holding `index.lock()` and `user_usage.write()` together
		// closes the dominant TOCTOU race (concurrent writers cannot create a
		// new index entry under our held index lock; concurrent
		// `release_user_quota` only takes `user_usage.read()` which is
		// excluded). Build a live-sender set in one index pass so retain is
		// O(senders + entries) instead of O(senders × entries).
		{
			let index = self.index.lock();
			let mut usage = self.user_usage.write();
			let live: HashSet<&SenderId> = index.values().map(|m| &m.sender_id).collect();
			usage.retain(|sender_id, counter| {
				counter.load(Ordering::Relaxed) > 0 || live.contains(sender_id)
			});
		}

		// Let the rate limiter shed stale per-sender state on the same cadence.
		self.rate_limiter.evict_stale();

		total_freed
	}
```

**File:** substrate/client/cli/src/params/rpc_params.rs (L78-85)
```rust
	/// RPC rate limiting (calls/minute) for each connection.
	///
	/// This is disabled by default.
	///
	/// For example `--rpc-rate-limit 10` will maximum allow
	/// 10 calls per minute per connection.
	#[arg(long)]
	pub rpc_rate_limit: Option<NonZeroU32>,
```
