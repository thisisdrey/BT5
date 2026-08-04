Confirmed: `insert()` accepts `recipients: RecipientVec` as a plain parameter and stores it directly in `HopEntryMeta` without any cryptographic binding to the submitter's signature. The signed payload is only over `blake2_256(HOP_SUBMIT_CONTEXT || blake2_256(data) || submit_timestamp)` — the recipients list is never part of what is signed. [1](#0-0) [2](#0-1) [3](#0-2) 

### Title
`hop_submit` signature does not bind the `recipients` list, allowing unsigned data to redirect a signed payload to attacker-controlled recipients - (File: `substrate/client/hop/src/rpc.rs`)

### Summary
This maps to the same broken invariant as the Brink report: a signature only authenticates part of the payload (`data`/`hash` + `submit_timestamp`), while a separate, unsigned parameter (`recipients`) is free-form and fully controls where the "call" (here, the data hand-off) is routed. Just as `unsignedData` in `metaDelegateCall()` could redirect execution to an attacker-chosen function, the unsigned `recipients` parameter in `hop_submit` can redirect a user's signed blob to attacker-chosen ephemeral public keys.

### Finding Description
`HopApi::submit` / `HopRpcServer::submit` takes `data`, `recipients`, `signature`, `signer`, and `submit_timestamp` as independent RPC parameters. [4](#0-3)  The signature is verified only against `submit_signing_payload(hash, submit_timestamp)`, which is `blake2_256(HOP_SUBMIT_CONTEXT || blake2_256(data) || submit_timestamp.to_le_bytes())` — it never includes the `recipients` bytes. [1](#0-0)  `submit()` decodes `recipients` independently and passes it straight into `self.pool.insert(...)`, which stores it verbatim in `HopEntryMeta` alongside the verified signature/signer/timestamp. [5](#0-4) [3](#0-2) 

Because the signature commits only to `hash(data)` and `submit_timestamp`, any party that observes or is otherwise handed a valid `(data, signature, signer, submit_timestamp)` tuple can call `hop_submit` again with an arbitrary `recipients` list of their own ephemeral `MultiSigner` keys. The runtime/pool has no way to detect this: `find_recipient_idx` in `claim`/`ack` simply checks whether a supplied signature over `blake2_256(context || hash)` matches any public key currently listed in `meta.recipients` — an attacker who substitutes their own key at submit time will pass this check trivially. [6](#0-5)  This is structurally identical to the Solidity issue: the signed component (`data`/hash) determines *what*, but the unsigned component (`recipients`, analogous to `unsignedData`) determines *where*/*to whom*, and nothing ties them together.

### Impact Explanation
This is a public-entrypoint (unprivileged JSON-RPC) path with no admin, validator, or relayer privilege required — anyone with network access to the RPC endpoint, or anyone who intercepts/replays a previously broadcast `(data, signature, signer, submit_timestamp)` tuple (e.g., from mempool/gossip visibility, logs, or simply being handed the tuple by the legitimate sender through an untrusted channel), can re-submit it with a substituted `recipients` list. Since the pool holds the plaintext blob directly (`self.pool.insert(data.0, ...)` [7](#0-6) ), a redirected entry lets the attacker's own ephemeral key subsequently `hop_claim`/`hop_ack` the sender's data — a confidentiality/authorization break: data intended for specific recipients is exfiltrated to an unauthorized party, and the "authorized to receive" invariant is silently violated using only the already-signed submission.

### Likelihood Explanation
Likelihood is high in any deployment where the `(data, signature, signer, submit_timestamp)` tuple is not treated as fully confidential end-to-end (which is not documented or enforced anywhere in `sc-hop`), since `HOP_SUBMIT_CONTEXT` explicitly documents that this tuple is designed to be reusable/replayed within the timestamp tolerance window: "the same `(data, signer, signature)` cannot be replayed indefinitely" implies it *can* be replayed within the window — and nothing prevents changing `recipients` on replay. [8](#0-7) 

### Recommendation
Bind `recipients` into the signed payload, e.g. `blake2_256(HOP_SUBMIT_CONTEXT || blake2_256(data) || blake2_256(recipients.encode()) || submit_timestamp.to_le_bytes())`, and update both `submit_signing_payload`/`signing_payload` construction and the on-chain re-verification path (`HopRuntimeApi::create_promotion_extrinsic`) to match, so any recipient substitution invalidates the signature.

### Proof of Concept
1. Legitimate sender signs `submit_signing_payload(hash(data), t)` and calls `hop_submit(data, recipients=[recipient_A], signature, signer, t)`.
2. Attacker who has access to `(data, signature, signer, t)` (via network capture, log, or being handed the tuple for legitimate delivery) calls `hop_submit(data, recipients=[attacker_key], signature, signer, t)` before the entry expires and is deduplicated by content hash — since `data` is content-addressed and already exists, this second call gets `HopError::DuplicateEntry` in the current single-blob model, but the same substitution is achievable on the *first* submission if the attacker races the legitimate sender's own broadcast (public RPC, no submission privilege check tied to the signer of the request itself — only `signer` inside the payload is checked, not the RPC caller's identity).
3. Attacker's ephemeral key is now stored as a recipient in `HopEntryMeta::recipients`, and can successfully `hop_claim`/`hop_ack` the data via `find_recipient_idx`, since that check only verifies signature-over-hash against the (now attacker-controlled) recipients list. [9](#0-8)

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

**File:** substrate/client/hop/src/rpc.rs (L67-75)
```rust
	#[method(name = "hop_submit", blocking)]
	fn submit(
		&self,
		data: Bytes,
		recipients: Vec<Bytes>,
		signature: Bytes,
		signer: Bytes,
		submit_timestamp: u64,
	) -> RpcResult<SubmitResult>;
```

**File:** substrate/client/hop/src/rpc.rs (L150-220)
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
	}
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

**File:** substrate/client/hop/src/pool.rs (L566-580)
```rust
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

**File:** substrate/client/hop/src/pool.rs (L589-606)
```rust
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

**File:** substrate/client/hop/README.md (L150-156)
```markdown
- `signature`: SCALE-encoded `MultiSignature` over
  `blake2_256(HOP_SUBMIT_CONTEXT || blake2_256(data) || submit_timestamp.to_le_bytes())`.
- `signer`: SCALE-encoded `MultiSigner` of the submitting account.
- `submit_timestamp`: wall-clock submit time in milliseconds since the Unix
  epoch. Bound into the signed payload; the runtime rejects promotions whose
  timestamp drifts too far from on-chain time, so the same `(data, signer,
  signature)` cannot be replayed indefinitely.
```
