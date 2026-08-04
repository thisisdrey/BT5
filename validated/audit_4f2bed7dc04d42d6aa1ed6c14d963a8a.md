Investigation into the report's core pattern — expensive work occurring *before* rate‑limiting is applied — surfaces a concrete local analog in this repo's custom `hop` client crate (`substrate/client/hop`), which implements an off‑chain data hand-off queue exposed over the node's JSON‑RPC surface.

### Title
Per-account rate limiter in HOP data pool is bypassed for unauthorized `hop_submit` floods, allowing unmetered runtime state-read amplification - (File: `substrate/client/hop/src/rpc.rs`, `substrate/client/hop/src/pool.rs`)

### Summary
`HopRpcServer::submit` (the handler for the public `hop_submit` RPC method) performs SCALE decoding, a size check, and a **runtime API call** (`can_account_promote`, which reads chain state) *before* any per-account rate limiting is applied. The only per-account rate limiter (`RateLimiter::check`) lives inside `HopDataPool::insert`, which is called at the very end of the submit flow — and is **never reached** when the account fails the authorization check. This means an unauthenticated caller can spam `hop_submit` with any `signer`/account value and force the node to repeatedly execute a runtime API state read for every single request, completely unmetered, regardless of account validity.

### Finding Description
The handler order in `submit` is:
1. Decode `recipients: Vec<Bytes>` into `MultiSigner`s (bounded only by `MAX_RECIPIENTS` after collection) [1](#0-0) 
2. Decode `signer` and `signature` [2](#0-1) 
3. Check `data_len > runtime_max` (`max_promotion_size`) [3](#0-2) 
4. Call the **runtime API** `can_account_promote` to authorize the sender [4](#0-3) 
5. Only *after* authorization succeeds: verify the cryptographic submit signature [5](#0-4) 
6. Only *after* signature verification: call `self.pool.insert(...)`, which is the **only** place `RateLimiter::check` is invoked [6](#0-5) [7](#0-6) 

If `can_account_promote` returns `false` (i.e., the caller supplies an arbitrary/unauthorized account — trivial for an attacker to construct since `signer` is attacker-controlled and never proven to match a real submitting party until *after* this check), the function returns `Err(HopError::NotAuthorized)` immediately at step 4, **before** signature verification and **before** ever reaching `pool.insert`/`RateLimiter::check`. The doc comment even confirms the intent — "a flood of unauthorized requests must not force a signature verification per submit" — but the same reasoning was not applied to the runtime authorization call itself, which is a state-trie read via `CallApiAt`, i.e., non-trivial work relative to a simple bounds check.

Additionally, the module's own documentation claims two layers of protection: the node-global `--rpc-rate-limit` and the HOP per-account limiter [8](#0-7) . However the node-global limiter is **disabled by default** (`Option<NonZeroU32> = None`) [9](#0-8) , and the HOP-specific limiter — the only one actually gating this method — sits downstream of the expensive authorization call, so it provides no protection against this specific flood pattern.

### Impact Explanation
An unauthenticated remote caller can send an unbounded number of `hop_submit` requests, each carrying an arbitrary (unauthorized) `signer`, and force the node to execute a `can_account_promote` runtime API call (a state read against `best_hash`) per request with zero throttling. Because this runs through the same `CallApiAt`/wasm-runtime execution path used elsewhere in the node [10](#0-9) , sustained flooding degrades node responsiveness and burns CPU/runtime-executor capacity on a public RPC endpoint — the same "public underpriced work" pattern as the external report's memory-amplification issue, but manifesting as unmetered runtime-call amplification instead of JSON parsing amplification.

### Likelihood Explanation
Highly likely to be triggerable: `hop_submit` is a public, unauthenticated RPC method; the attacker needs no valid account, no signature, and no special network position — only network access to an RPC node with `--enable-hop` set. Constructing an arbitrary unauthorized `MultiSigner` costs nothing (an attacker can just generate a fresh keypair). Bypassing the rate limiter requires no race condition — it's a straightforward consequence of the code's control flow ordering.

### Recommendation
Move the per-account rate-limit check to occur before the runtime authorization call (and certainly before signature verification), keyed on the caller-supplied `signer`/account (or, if that is considered untrusted pre-verification, apply a coarse pre-check such as per-connection/per-IP throttling before dispatching any runtime API call). Alternatively, restructure `RateLimiter::check` to run as the very first gate in `HopRpcServer::submit`, consistent with the stated design intent in the module doc comment.

### Proof of Concept
1. Start a node with `--enable-hop` (HOP RPC enabled), rate limiting left at defaults (`--rpc-rate-limit` unset → disabled globally).
2. Repeatedly call `hop_submit(data, recipients, signature, signer, submit_timestamp)` over HTTP/WS with:
   - A small valid `data` blob (under `max_promotion_size`).
   - A single, freshly generated `MultiSigner` as both `recipients[0]` and `signer` (never registered/authorized on-chain).
   - Any syntactically valid but unrelated `signature` bytes (verification is never reached because authorization fails first).
3. Each call reaches step 4 in `submit`, executing `can_account_promote` (a runtime API state read) and then returns `NotAuthorized` — never touching `HopDataPool::insert`/`RateLimiter::check`.
4. Because the per-account rate limiter and the (default-disabled) global RPC rate limiter never observe these requests being throttled at the point that matters, the caller can issue requests as fast as the transport allows, each forcing a runtime API invocation, with no backoff signal from the per-account bucket in `RateLimiter::check` [11](#0-10) .

### Citations

**File:** substrate/client/hop/src/rpc.rs (L19-22)
```rust
//! Two layers of rate limiting apply:
//! - The node's global per-connection limit configured via `--rpc-rate-limit`.
//! - HOP-specific per-account token buckets (request rate + bandwidth) enforced inside the pool;
//!   see [`crate::rate_limit`] and the `--hop-*-rate` / `--hop-*-burst` CLI flags.
```

**File:** substrate/client/hop/src/rpc.rs (L24-37)
```rust
use crate::{
	pool::HopDataPool,
	runtime_api,
	types::{
		submit_signing_payload, HopError, HopHash, PoolStatus, Recipient, RecipientVec,
		SubmitResult, MAX_RECIPIENTS,
	},
};
use codec::Decode;
use jsonrpsee::{
	core::{async_trait, RpcResult},
	proc_macros::rpc,
};
use sp_api::CallApiAt;
```

**File:** substrate/client/hop/src/rpc.rs (L158-170)
```rust
		let recipient_keys: RecipientVec = recipients
			.into_iter()
			.map(|r| {
				MultiSigner::decode(&mut &r.0[..])
					.map(|signer| Recipient { signer, claimed: false })
					.map_err(|_| HopError::InvalidRecipientKey)
			})
			.collect::<Result<Vec<_>, _>>()?
			.try_into()
			.map_err(|v: Vec<Recipient>| HopError::TooManyRecipients {
				provided: v.len(),
				limit: MAX_RECIPIENTS as usize,
			})?;
```

**File:** substrate/client/hop/src/rpc.rs (L172-175)
```rust
		let signer =
			MultiSigner::decode(&mut &signer.0[..]).map_err(|_| HopError::InvalidSigner)?;
		let multi_sig = MultiSignature::decode(&mut &signature.0[..])
			.map_err(|_| HopError::InvalidSignature)?;
```

**File:** substrate/client/hop/src/rpc.rs (L182-189)
```rust
		// Reject oversized payloads before the per-account authorization lookup so
		// a flood of too-big submits cannot force runtime state reads. The cap is
		// the runtime-declared `max_promotion_size`; the runtime is authoritative.
		let runtime_max = runtime_api::max_promotion_size::<Block, _>(&*self.client, best_hash)
			.map_err(HopError::from)?;
		if data_len > runtime_max as usize {
			return Err(HopError::DataTooLarge(data_len, runtime_max).into());
		}
```

**File:** substrate/client/hop/src/rpc.rs (L191-205)
```rust
		// Check authorization before verifying the signature: a flood of unauthorized
		// requests must not force a signature verification per submit.
		// `can_account_promote` returns false for any reason the runtime rejects:
		// unauthorized account or exhausted per-account quota.
		let account_id: AccountId32 = signer.clone().into_account();
		let authorized = runtime_api::can_account_promote::<Block, _>(
			&*self.client,
			best_hash,
			account_id.clone(),
			data_len as u32,
		)
		.map_err(HopError::from)?;
		if !authorized {
			return Err(HopError::NotAuthorized.into());
		}
```

**File:** substrate/client/hop/src/rpc.rs (L207-214)
```rust
		// Domain-separated payload so a submit signature cannot be replayed as claim/ack,
		// and bound to `submit_timestamp` so an old signature can't be replayed long
		// after the fact (the runtime enforces a tolerance window on the timestamp).
		let hash = H256(blake2_256(&data.0));
		let submit_payload = submit_signing_payload(&hash, submit_timestamp);
		if !multi_sig.verify(&submit_payload[..], &account_id) {
			return Err(HopError::InvalidSignature.into());
		}
```

**File:** substrate/client/hop/src/rpc.rs (L216-219)
```rust
		let sender_id: [u8; 32] = account_id.into();
		self.pool
			.insert(data.0, recipient_keys, sender_id, signer, multi_sig, submit_timestamp)?;
		Ok(SubmitResult { pool_status: self.pool.status() })
```

**File:** substrate/client/hop/src/pool.rs (L373-376)
```rust
		// Rejected requests never reserve capacity — check before any atomic bump.
		if let Err(retry_after_secs) = self.rate_limiter.check(&sender_id, accounted) {
			return Err(HopError::RateLimited { retry_after_secs });
		}
```

**File:** substrate/client/cli/src/config.rs (L364-367)
```rust
	/// RPC rate limit configuration.
	fn rpc_rate_limit(&self) -> Result<Option<NonZeroU32>> {
		Ok(None)
	}
```

**File:** substrate/client/hop/src/rate_limit.rs (L147-171)
```rust
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
