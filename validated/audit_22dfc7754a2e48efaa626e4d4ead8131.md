## Summary

Confirmed local analog: the HOP (Hand-Off Protocol) submit-signature payload binds only the data hash and timestamp — never the recipient list — while the RPC layer (attacker-reachable, unauthenticated w.r.t. request tampering) accepts the recipients as separate, unsigned parameters and stores them as the authoritative delivery list.

### Title
HOP `hop_submit` signature does not bind the `recipients` list, allowing recipient substitution after signing - (File: `substrate/client/hop/src/rpc.rs`, `substrate/client/hop/src/types.rs`)

### Summary
`submit_signing_payload` in `substrate/client/hop/src/types.rs` hashes only `HOP_SUBMIT_CONTEXT || blake2_256(data) || submit_timestamp`, deliberately excluding the `recipients` parameter of `hop_submit`. [1](#0-0)  The RPC handler `HopRpcServer::submit` decodes `recipients: Vec<Bytes>` separately from `signature`/`signer`, verifies the signature only against the hash/timestamp payload, and then persists the caller-supplied `recipient_keys` verbatim into `HopEntryMeta` as the set of parties allowed to `hop_claim`/`hop_ack` the private data. [2](#0-1)  This is the same broken-invariant class as the reported `CalculateRequestHash` bug: a field that materially changes the meaning/authorization of a signed request (`QuorumIDs` there, `recipients` here) is excluded from the hash that is actually signed and verified.

### Finding Description
`hop_submit`'s wire contract is `(data, recipients, signature, signer, submit_timestamp)`. The signature is checked with:
```
let hash = H256(blake2_256(&data.0));
let submit_payload = submit_signing_payload(&hash, submit_timestamp);
if !multi_sig.verify(&submit_payload[..], &account_id) { ... }
``` [3](#0-2) 
Neither `recipients` nor any hash of `recipients` enters `submit_payload`. The `recipients` bytes are decoded and turned into `RecipientVec` independently, then stored in `HopEntryMeta.recipients`, which is the sole authorization set later checked by `find_recipient_idx` for both `hop_claim` and `hop_ack`. [4](#0-3) 

Because the sender's cryptographic signature only commits to `blake2_256(data)` and `submit_timestamp`, any party that can intercept or re-relay a `hop_submit` call before it reaches the node (a malicious/compromised RPC proxy, a MITM on an unencrypted RPC channel, or a node operator relaying on behalf of light clients) can strip the original `recipients` list and substitute a different one while keeping the sender's untouched, valid signature and the untouched `data`. The forged request still passes `multi_sig.verify` because the payload it's checked against never mentions recipients.

### Impact Explanation
HOP is the private handoff channel: `data` is delivered only to accounts whose `MultiSigner` is present in `recipients`, and `hop_claim`/`hop_ack` gate access purely on producing a signature from one of those listed keys. [5](#0-4)  If `recipients` can be swapped without invalidating the sender's signature, an attacker can:
- Redirect the sender's private data to an attacker-controlled key (recipients substitution → confidentiality break / wrong-beneficiary delivery, matching the "wrong beneficiary" pivot).
- Add extra unauthorized recipients to a legitimate submission, since `insert()` only validates recipient list well-formedness (non-empty, no duplicates, size bound) and never that it matches what the sender intended. [6](#0-5) 

The corrupted value is `HopEntryMeta.recipients`, which is the exact field driving claim/ack authorization but is excluded from the value covered by `HopEntryMeta.signature`/`submit_timestamp`'s payload.

### Likelihood Explanation
This does not require a malicious validator/collator/relayer with special protocol trust — it requires only interception or substitution of an RPC call in transit (e.g., an untrusted/compromised RPC gateway, proxy, or a node operator forwarding third-party `hop_submit` calls), which is a normal deployment topology for public RPC endpoints. No signature forgery is needed, only reordering of an unsigned parameter alongside a valid signature — this is a low-effort, high-determinism tampering path once request interception is possible, directly mirroring the original `CalculateRequestHash`/`QuorumIDs` scenario (hash excludes a field that gets used downstream for a security-relevant decision).

### Recommendation
Include a commitment to `recipients` in `submit_signing_payload` (e.g. hash the SCALE-encoded, canonically-ordered `RecipientVec` and mix it into the blake2_256 payload alongside `data`'s hash and `submit_timestamp`), and have the pool's `insert()` re-derive/verify that the persisted `recipients` matches this hash before accepting the entry. This is analogous to the report's fix of folding `QuorumIDs` into the churn request hash.

### Proof of Concept
1. Sender computes `sig = sign(submit_signing_payload(blake2_256(data), ts))` and calls `hop_submit(data, recipients=[Alice_pk], sig, signer, ts)` via an RPC proxy/relay it does not fully trust.
2. The intercepting party replaces `recipients` with `[Attacker_pk]`, forwarding the same `data`, `sig`, `signer`, `ts` unchanged, and submits to the node.
3. `HopRpcServer::submit` computes `submit_payload` from `data`/`ts` only, verification succeeds (`multi_sig.verify` passes because `recipients` never entered the payload), and `pool.insert()` stores `HopEntryMeta { recipients: [Attacker_pk], ... }`. [7](#0-6) 
4. Attacker calls `hop_claim(hash, sign(claim_payload, Attacker_key))`; `find_recipient_idx` matches `Attacker_pk` in `meta.recipients` and returns the sender's private `data`. [8](#0-7)  The intended recipient Alice never receives it and has no signal that the recipient list was altered.

### Citations

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

**File:** substrate/client/hop/src/rpc.rs (L150-219)
```rust
	fn submit(
		&self,
		data: Bytes,
		recipients: Vec<Bytes>,
		signature: Bytes,
		signer: Bytes,
		submit_timestamp: u64,
	) -> RpcResult<SubmitResult> {
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

		let signer =
			MultiSigner::decode(&mut &signer.0[..]).map_err(|_| HopError::InvalidSigner)?;
		let multi_sig = MultiSignature::decode(&mut &signature.0[..])
			.map_err(|_| HopError::InvalidSignature)?;

		let chain_info = self.client.info();
		let best_hash = chain_info.best_hash;

		let data_len = data.0.len();

		// Reject oversized payloads before the per-account authorization lookup so
		// a flood of too-big submits cannot force runtime state reads. The cap is
		// the runtime-declared `max_promotion_size`; the runtime is authoritative.
		let runtime_max = runtime_api::max_promotion_size::<Block, _>(&*self.client, best_hash)
			.map_err(HopError::from)?;
		if data_len > runtime_max as usize {
			return Err(HopError::DataTooLarge(data_len, runtime_max).into());
		}

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

		// Domain-separated payload so a submit signature cannot be replayed as claim/ack,
		// and bound to `submit_timestamp` so an old signature can't be replayed long
		// after the fact (the runtime enforces a tolerance window on the timestamp).
		let hash = H256(blake2_256(&data.0));
		let submit_payload = submit_signing_payload(&hash, submit_timestamp);
		if !multi_sig.verify(&submit_payload[..], &account_id) {
			return Err(HopError::InvalidSignature.into());
		}

		let sender_id: [u8; 32] = account_id.into();
		self.pool
			.insert(data.0, recipient_keys, sender_id, signer, multi_sig, submit_timestamp)?;
		Ok(SubmitResult { pool_status: self.pool.status() })
```

**File:** substrate/client/hop/src/pool.rs (L341-356)
```rust
	pub fn insert(
		&self,
		data: Vec<u8>,
		recipients: RecipientVec,
		sender_id: SenderId,
		signer: MultiSigner,
		signature: MultiSignature,
		submit_timestamp: u64,
	) -> Result<HopHash, HopError> {
		if recipients.is_empty() {
			return Err(HopError::NoRecipients);
		}
		let unique: BTreeSet<&MultiSigner> = recipients.iter().map(|r| &r.signer).collect();
		if unique.len() != recipients.len() {
			return Err(HopError::DuplicateRecipient);
		}
```

**File:** substrate/client/hop/src/pool.rs (L561-580)
```rust
	/// Decode `signature` and return the index of the matching recipient in
	/// `meta.recipients`. `context` is the operation's domain separator (claim
	/// / ack). Returning an index keeps a single implementation for both
	/// shared- and exclusive-borrow callers (`meta.recipients[idx]` works in
	/// either case).
	fn find_recipient_idx(
		meta: &HopEntryMeta,
		hash: &HopHash,
		signature: &[u8],
		context: &[u8],
	) -> Result<usize, HopError> {
		let multi_sig =
			MultiSignature::decode(&mut &signature[..]).map_err(|_| HopError::InvalidSignature)?;
		let payload = signing_payload(context, hash);

		meta.recipients
			.iter()
			.position(|r| multi_sig.verify(&payload[..], &r.signer.clone().into_account()))
			.ok_or(HopError::NotRecipient)
	}
```

**File:** substrate/client/hop/src/pool.rs (L582-606)
```rust
	/// Claim data from the pool (read-only). Verifies the signature against recipient
	/// public keys. Returns the data if the signature matches a recipient.
	///
	/// This does NOT mark the recipient as claimed — call `ack` after receiving the data
	/// to confirm receipt.
	///
	/// Returns `AlreadyClaimed` if the recipient has already acked (data may be deleted).
	pub fn claim(&self, hash: &HopHash, signature: &[u8]) -> Result<Vec<u8>, HopError> {
		{
			let index = self.index.lock();
			let meta = index.get(hash).ok_or(HopError::NotFound)?;
			// Map NotRecipient → NotFound so callers cannot probe whether a hash
			// exists by observing different error codes.
			let idx = Self::find_recipient_idx(meta, hash, signature, HOP_CLAIM_CONTEXT)
				.map_err(|_| HopError::NotFound)?;

			// If this recipient already acked, the data may be gone.
			if meta.recipients[idx].claimed {
				return Err(HopError::AlreadyClaimed);
			}
		}
		// Read blob from disk and verify its content hash. May be gone if
		// concurrently acked and deleted, in which case we surface NotFound.
		self.read_and_verify_blob(hash)
	}
```
