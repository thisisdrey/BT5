### Title
HOP per-account submit rate limiter and per-user quota are keyed on a caller-supplied ephemeral signing key, so an unprivileged sender bypasses both by rotating keys - ([File: substrate/client/hop/src/rate_limit.rs])

### Summary
The external report's core broken invariant is: a throttling control is scoped to a caller-controlled, freely-rotatable identity (browser cookie/profile) instead of a persistent, costly identity (IP), so the attacker defeats the control by discarding and recreating that identity at zero cost. The local analog is the HOP node service's per-account rate limiter and per-user quota, both keyed by `SenderId`, which is derived directly from the `signer` (`MultiSigner`) supplied in the `hop_submit` request rather than any registered, costly on-chain identity.

### Finding Description
`RateLimiter::check` in `substrate/client/hop/src/rate_limit.rs` enforces token-bucket limits keyed by `SenderId`: [1](#0-0) 

`SenderId` is defined as "Sender identity derived from the account that signed the submission" — a raw 32-byte value with no binding to an on-chain account, balance, stake, or registered identity: [2](#0-1) 

In `HopDataPool::insert`, the rate-limit check and the per-user quota (`charge_user`) are both keyed on this same caller-supplied `sender_id`, and the check happens purely against local in-memory/token-bucket state before any capacity is reserved: [3](#0-2) 

Because `signer`/`sender_id` for `hop_submit` is an ephemeral keypair the caller generates locally (the docs describe recipients as "ephemeral public keys the sender generated for this handoff," and the submitter's own `signer`/`signature` are likewise self-supplied and only checked for a valid signature over the submitted payload, not for membership in any registered/funded account set), generating a fresh keypair is free and instantaneous. This is exactly the "switch browser profile" primitive from the external report: the defender's throttle recognizes and blocks identity A, but identity B — created for free by the same attacker from the same machine — sails through with a brand-new token bucket (`get_or_create` in `rate_limit.rs`) and a brand-new user-quota counter (`user_usage` map in `pool.rs`), because both are `HashMap`s keyed purely by the attacker-chosen 32-byte value.

### Impact Explanation
This lets a single attacker, from a single machine, exceed the intended per-account submit rate (`--hop-submit-rate-per-min`, `--hop-submit-burst`), per-account bandwidth (`--hop-bandwidth-per-min-mib`), and per-account pool quota (`--hop-max-user-size`) by simply generating a new ed25519/sr25519/ecdsa keypair for every request. Since these limits are the only defense (besides the global pool cap `--hop-max-pool-size` and node-global RPC rate limit) against filling the shared HOP disk-backed pool, an attacker can use up the entire pool capacity, evict/deny service to legitimate users' submissions, and drive maintenance-task promotion churn — all while appearing as an unbounded number of distinct "accounts," each individually under every configured per-account cap. This is public underpriced work: cheap, unauthenticated (no real account cost) requests degrade a shared node resource that also feeds on-chain promotion via the maintenance task.

### Likelihood Explanation
High. No privileged, malicious-peer, or governance assumptions are needed — this is exploitable directly against the public `hop_submit` RPC method by an ordinary unauthenticated caller who can generate keypairs locally. `RateLimitConfig::disabled()` and the node-global RPC limiter are the only other layers; neither prevents identity rotation at the HOP-application layer, and the `--hop-disable-rate-limit` flag is dev/test only (limiter is enabled by default with sane bursts), meaning the bypass is available in the default deployed configuration.

### Recommendation
Bind `SenderId`/rate-limiting and quota accounting to something with real cost to acquire — e.g., require the submitting `signer` to correspond to a funded/registered on-chain account (consistent with the existing `HopRuntimeApi::can_account_promote(who, data_len)` authorization hook design intent) and verify that authorization *before* consulting the rate limiter or quota map, rather than trusting a caller-chosen ephemeral key as the rate-limit identity. Additionally, add an IP- or connection-level secondary limiter in the RPC layer so that even distinct `SenderId`s from the same network peer are throttled in aggregate, mirroring the external report's recommended escalation from per-session to per-IP blocking.

### Proof of Concept
1. Configure a node with `--enable-hop` and default rate-limit settings (`--hop-submit-rate-per-min 60`, `--hop-submit-burst 120`).
2. From a single machine/IP, call `hop_submit` repeatedly using signer key `K1` until `RateLimiter::check` returns `Err` (i.e., after the burst of 120 within the window), confirming rejection with `HopError::RateLimited`.
3. Generate a fresh keypair `K2` (trivial, offline, free) and immediately call `hop_submit` again with `signer = K2`. Because `RateLimiter::get_or_create` (`substrate/client/hop/src/rate_limit.rs:132-141`) creates a brand-new `UserRateState` for any unseen `SenderId`, and `HopDataPool::charge_user` (`substrate/client/hop/src/pool.rs:266-278`) creates a brand-new zero usage counter for `K2`, the submission is admitted with a full fresh burst/quota.
4. Repeat step 3 indefinitely with `K3, K4, ...` to fill the shared pool up to `--hop-max-pool-size` and exhaust node resources, despite each individual "account" staying within its configured per-account limits.

### Citations

**File:** substrate/client/hop/src/rate_limit.rs (L132-153)
```rust
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
```

**File:** substrate/client/hop/src/types.rs (L32-34)
```rust
/// Sender identity derived from the account that signed the submission.
pub type SenderId = [u8; 32];

```

**File:** substrate/client/hop/src/pool.rs (L362-387)
```rust
		let data_len = data.len() as u64;

		// Total accounted size includes bounded per-recipient metadata overhead so
		// a submitter cannot inflate memory via large recipient lists while the
		// capacity counter only tracks `data.len()`. Charge the rate limiter the
		// same accounted size, otherwise a 1-byte payload with 256 recipients
		// would cost ~10 KiB of pool capacity while only spending 1 byte of
		// bandwidth tokens — making the bandwidth dimension non-functional for
		// fan-out-heavy entries.
		let accounted = entry_accounted_size(data_len, recipients.len());

		// Rejected requests never reserve capacity — check before any atomic bump.
		if let Err(retry_after_secs) = self.rate_limiter.check(&sender_id, accounted) {
			return Err(HopError::RateLimited { retry_after_secs });
		}

		let previous_size = self.current_size.fetch_add(accounted, Ordering::Relaxed);
		if previous_size.saturating_add(accounted) > self.max_size {
			self.current_size.fetch_sub(accounted, Ordering::Relaxed);
			return Err(HopError::PoolFull(previous_size, self.max_size));
		}

		if let Err(e) = self.charge_user(&sender_id, accounted) {
			self.current_size.fetch_sub(accounted, Ordering::Relaxed);
			return Err(e);
		}
```
