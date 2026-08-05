## Analysis

The seeded HOP (Hand-Off Protocol) node service is the local analog. The report's core broken invariant — "signed application payload lacks a domain separator binding it to chain identity, contract/target, and function" — reappears in `submit_signing_payload`.

### Title
Cross-chain replay of `hop_submit` promotion-consent signatures due to missing chain/genesis binding in the signed payload - (File: `substrate/client/hop/src/types.rs`)

### Summary
The `hop_submit` signature that authorizes on-chain promotion of a user's off-chain HOP blob is computed as `blake2_256(HOP_SUBMIT_CONTEXT || blake2_256(data) || submit_timestamp.to_le_bytes())` [1](#0-0) . This payload is domain-separated from `claim`/`ack` contexts, and time-bound via `submit_timestamp`, but it contains no chain-identifying material (no genesis hash, no spec/tx version, no runtime-specific salt). The same `(data, signer, signature, submit_timestamp)` tuple that is a valid, publicly-observable consent artifact on one HOP-enabled chain is therefore also a valid consent artifact on any other HOP-enabled chain sharing the same `AccountId` scheme.

### Finding Description
`hop_submit` verifies the signature purely against the signer's public key and the domain-separated, chain-agnostic payload [2](#0-1) . Nothing in the signed bytes ties the consent to a specific chain: `HOP_SUBMIT_CONTEXT` is a fixed literal `b"hop-submit-v1:"` shared by every deployment of `sc-hop` [3](#0-2) , and the rest of the payload is just `blake2_256(data)` and the wall-clock `submit_timestamp`.

Once an entry is promoted, the tuple `(signer, signature, submit_timestamp)` is carried verbatim into the unsigned promotion extrinsic via `HopRuntimeApi::create_promotion_extrinsic` [4](#0-3)  and becomes public, permanent on-chain data. The maintenance task retrieves exactly this authorization triple from local storage to build the extrinsic [5](#0-4) , and `HopEntryMeta` documents that the runtime pallet "re-verifies the submit signature using this key when the unsigned promotion extrinsic lands on-chain" [6](#0-5) .

Because `AccountId32`/`MultiSigner` key material is chain-agnostic (the same sr25519/ed25519/ecdsa keypair produces the same account on every Substrate chain), an attacker who observes a promoted `(data, signer, signature, submit_timestamp)` triple on chain A — trivially, by reading chain A's block explorer/state — can resubmit the identical bytes to `hop_submit` on chain B. `hop_submit` on B will:
1. Pass size/authorization checks if B's `can_account_promote` policy also authorizes that account (common for shared allowlists, e.g. the same governance/foundation account permitted on multiple system parachains, or an initially-permissive default policy) [7](#0-6) .
2. Pass signature verification, since the payload never encoded which chain the signer intended to authorize [8](#0-7) .
3. Insert the entry and eventually get promoted on-chain, causing chain B's runtime to accept and store data as "consented-to by the signer" for B specifically — even though the signer only ever intended to authorize storage on chain A.

The `submit_timestamp` freshness check only bounds *how long* a signature is replayable, not *where* — it does nothing to stop a nearly-immediate cross-chain replay, and the sc-hop code and doc explicitly acknowledge only the temporal replay risk ("old `(data, signer, signature)` tuples cannot be replayed indefinitely" [9](#0-8) ) while never addressing cross-chain reuse — precisely the EIP-712 domain-separator gap called out in the external report (no chain ID / verifying-contract equivalent bound into the signed bytes).

### Impact Explanation
This falls under "forged or mis-bound proof or state acceptance" / "unauthorized execution or origin escalation" from the impact gate: a signature that a user produced to consent to promotion on chain A is mis-bound and accepted as valid consent on chain B, causing chain B's runtime to store attacker-chosen (but victim-signed) content and to attribute it to the victim's account without the victim ever authorizing that specific chain. This can be leveraged to force unwanted permanent on-chain storage costs/state onto a victim account, or — depending on what the runtime does with the promoted data/authorization event downstream — to trigger account-linked side effects on a chain the victim never intended to interact with.

### Likelihood Explanation
Exploitation requires no privileged actor: any unprivileged party that can observe a promoted HOP entry (public on-chain data by design) and has access to a second HOP-enabled chain where the same account is authorized (a realistic, common configuration for shared allowlists/foundation accounts across a family of parachains) can perform the replay purely through the public `hop_submit` RPC. No relayer, validator, collator, or admin compromise is needed — this is a pure protocol/signing-scheme flaw in the client crate that ships with `polkadot-omni-node-lib` per the PR description [10](#0-9) .

### Recommendation
Bind the signed payload to chain identity, following the EIP-712-style mitigation recommended in the source report: include the runtime's genesis hash (or a per-runtime salt) and, ideally, the local account's on-chain nonce/spec version inside `submit_signing_payload`, e.g. `blake2_256(HOP_SUBMIT_CONTEXT || genesis_hash || blake2_256(data) || submit_timestamp.to_le_bytes())`, and have `HopRuntimeApi::create_promotion_extrinsic`'s runtime-side re-verification reject any promotion whose embedded genesis hash does not match the local chain.

### Proof of Concept
1. Deploy two HOP-enabled chains, A and B, both configuring `can_account_promote` to authorize the same `AccountId` (e.g. a shared foundation/testing account allowlisted on both, or a permissive default policy).
2. Victim calls `hop_submit(data, recipients, signature, signer, submit_timestamp)` on chain A; entry is later promoted on-chain per `HopMaintenanceTask::tick` [11](#0-10) , exposing `(data, signer, signature, submit_timestamp)` publicly in A's chain state.
3. Attacker extracts that tuple from A's public state and calls `hop_submit(data, recipients', signature, signer, submit_timestamp)` against chain B within the timestamp tolerance window.
4. Chain B's `rpc.rs::submit` verifies the signature successfully — `submit_signing_payload` is identical on both chains because it never encoded chain identity [2](#0-1)  — and the entry is accepted/promoted on B under the victim's account without any B-specific consent.

### Citations

**File:** substrate/client/hop/src/types.rs (L73-85)
```rust
	/// `MultiSigner` of the account that signed the submission. The runtime pallet
	/// re-verifies the submit signature using this key when the unsigned promotion
	/// extrinsic lands on-chain.
	pub signer: MultiSigner,
	/// The user's `hop_submit` signature over `submit_signing_payload(blake2_256(data),
	/// submit_timestamp)`. Carried along for the runtime to re-verify; "submit implies
	/// consent to promote" is the protocol semantic.
	pub signature: MultiSignature,
	/// Submit-time wall-clock timestamp (ms since unix epoch) bound into the
	/// signing payload. The runtime rejects promotions whose timestamp is too far
	/// from on-chain time, so old `(data, signer, signature)` tuples cannot be
	/// replayed indefinitely.
	pub submit_timestamp: u64,
```

**File:** substrate/client/hop/src/types.rs (L298-299)
```rust
/// Domain-separator prefix for `hop_submit` signatures.
pub const HOP_SUBMIT_CONTEXT: &[u8] = b"hop-submit-v1:";
```

**File:** substrate/client/hop/src/types.rs (L317-329)
```rust
/// Compute the 32-byte payload signed at `hop_submit` time.
///
/// The runtime pallet re-derives this exact byte sequence to verify the
/// signature on-chain, so the construction must remain byte-identical to the
/// pallet's `signing_payload(data, submit_timestamp)`:
/// `blake2_256(HOP_SUBMIT_CONTEXT || blake2_256(data) || submit_timestamp.to_le_bytes())`.
pub fn submit_signing_payload(hash: &HopHash, submit_timestamp: u64) -> [u8; 32] {
	let mut buf = [0u8; HOP_SUBMIT_CONTEXT.len() + 32 + 8];
	buf[..HOP_SUBMIT_CONTEXT.len()].copy_from_slice(HOP_SUBMIT_CONTEXT);
	buf[HOP_SUBMIT_CONTEXT.len()..HOP_SUBMIT_CONTEXT.len() + 32].copy_from_slice(hash.as_bytes());
	buf[HOP_SUBMIT_CONTEXT.len() + 32..].copy_from_slice(&submit_timestamp.to_le_bytes());
	blake2_256(&buf)
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

**File:** substrate/client/hop/src/rpc.rs (L206-214)
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

**File:** substrate/primitives/hop/src/lib.rs (L47-63)
```rust
		/// Construct an unsigned promotion extrinsic carrying the user's submit-time
		/// (in milliseconds from the Unix epoch), signer, signature, and timestamp
		/// so the runtime pallet can verify consent on-chain.
		///
		/// `submit_timestamp` is bound into the signed payload. Implementing
		/// runtimes **must** reject promotions whose timestamp is outside a
		/// tolerance window around the current on-chain clock — otherwise the
		/// same `(data, signer, signature)` tuple can be replayed indefinitely
		/// from the collator's persisted metadata. The width of the window is a
		/// runtime policy decision (clock skew + max acceptable promotion
		/// latency); a few hours is a reasonable upper bound.
		fn create_promotion_extrinsic(
			data: alloc::vec::Vec<u8>,
			signer: sp_runtime::MultiSigner,
			signature: sp_runtime::MultiSignature,
			submit_timestamp: u64,
		) -> Block::Extrinsic;
```

**File:** substrate/client/hop/src/promotion.rs (L235-279)
```rust
	/// Execute a single maintenance cycle: promote near-expiry entries and clean up expired ones.
	pub fn tick(&self) {
		let current_block = (self.best_block)();

		// Promote near-expiry entries one at a time to bound peak memory.
		if let Some(ref promoter) = self.promoter {
			const PROMOTION_BATCH_SIZE: usize = 10;
			let hashes =
				self.hop_pool
					.get_promotable(current_block, self.buffer_secs, PROMOTION_BATCH_SIZE);
			for hash in hashes {
				// First, ask the runtime whether this hash is already on-chain.
				// If so, the previous attempt (or a third party) already
				// landed it — flag locally and stop touching the chain.
				match promoter.is_promoted_on_chain(hash.as_fixed_bytes()) {
					Ok(true) => {
						self.hop_pool.mark_promoted(&hash);
						tracing::info!(
							target: "hop",
							hash = ?hex::encode(hash),
							"HOP entry already on-chain — flagged locally"
						);
						continue;
					},
					Ok(false) => {},
					Err(e) => {
						// Treat runtime-API failures as "unknown", which means
						// proceed with submission. Worst case we resubmit a
						// duplicate; the on-chain check will catch it next cycle.
						tracing::warn!(
							target: "hop",
							hash = ?hex::encode(hash),
							error = %e,
							"is_promoted_on_chain failed; assuming not on-chain"
						);
					},
				}

				let (data, signer, signature, submit_timestamp) =
					match self.hop_pool.get_with_auth(&hash) {
						Some(t) => t,
						None => continue,
					};
				let size = data.len();
				let result = promoter.promote(data, signer, signature, submit_timestamp);
```

**File:** prdoc/stable2606/pr_11662.prdoc (L66-74)
```text
crates:
  - name: sc-hop
    bump: patch
  - name: sp-hop
    bump: patch
  - name: polkadot-omni-node-lib
    bump: major
  - name: polkadot-sdk
    bump: minor
```
