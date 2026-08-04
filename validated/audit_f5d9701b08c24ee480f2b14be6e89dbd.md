## Finding [1](#0-0) 

### Title
HOP submit/claim/ack signing payload lacks a chain domain separator, enabling cross-chain signature replay - (File: `substrate/client/hop/src/types.rs`)

### Summary
The HOP (Hand-Off Protocol) node service signs `hop_submit`, `hop_claim`, and `hop_ack` operations with a digest built only from a static context prefix and the content hash (plus, for submit, a wall-clock timestamp). No chain-identifying value — genesis hash, spec version, or any chain/instance identifier — is folded into the signed payload. Because Substrate accounts (`sr25519`/`ed25519`/`ecdsa` `MultiSigner`/`MultiSignature`) are chain-agnostic, a signature produced for one HOP-enabled chain/collator is byte-identical to what would be required on any other HOP-enabled chain running the same client code, and can be replayed verbatim.

### Finding Description
`signing_payload` and `submit_signing_payload` compute:

```
blake2_256(context || hash)
blake2_256(HOP_SUBMIT_CONTEXT || blake2_256(data) || submit_timestamp.to_le_bytes())
``` [2](#0-1) 

`HOP_SUBMIT_CONTEXT`, `HOP_CLAIM_CONTEXT`, and `HOP_ACK_CONTEXT` are fixed byte strings shared by every deployment of the `sc-hop`/`sp-hop` crates [3](#0-2) . This mirrors exactly the bug class in the external report: the "quote digest" analog here (the HOP submit/claim/ack digest) commits to the operation type, the data hash, and (for submit) a timestamp, but never to the chain's own identity (genesis hash / spec version / any per-deployment salt).

The RPC layer verifies the submit signature against this payload and then persists `(signer, signature, submit_timestamp)` for later on-chain re-verification via the promotion extrinsic: [4](#0-3) 

The `HopRuntimeApi::create_promotion_extrinsic` doc explicitly states the runtime pallet must re-verify this exact signature on-chain and only bounds replay via a timestamp tolerance window — not via any chain-specific value: [5](#0-4) 

The claim/ack recipient lookup (`find_recipient_idx`) uses the same `signing_payload(context, hash)` construction with no chain binding either: [6](#0-5) 

Because the digest is identical across any two HOP deployments (same accountId space, same context constants, same content hash), a signature captured from one HOP-enabled chain (e.g. a public submission observed via RPC, or a signature a user legitimately produced on chain A) is valid and accepted verbatim by any other HOP-enabled chain/collator that the signer's account is also active on — there is no mechanism (chain ID, genesis hash, or otherwise) that scopes the signature to a single chain instance.

### Impact Explanation
This breaks the invariant that a HOP submit/claim/ack authorization is single-instance. An attacker who observes a valid `(data, signer, signature, submit_timestamp)` tuple from chain A (trivial, since `hop_submit` and its inputs are visible over public JSON-RPC and the eventual on-chain promotion extrinsic) can replay it against chain B's HOP pool/pallet within the timestamp tolerance window, causing:
- Duplicate promotion of the same content to on-chain storage on a second, unrelated chain without the signer's intent on that chain, consuming that account's per-chain `can_account_promote` authorization/quota there.
- Impersonation of the signer's consent for `hop_claim`/`hop_ack` operations against a different chain's pool if ephemeral recipient keys or accounts are reused across environments (e.g., testnet/mainnet sharing key material, or multiple parachains under the same relay/account model).

This matches the "forged or mis-bound proof or state acceptance" and "unauthorized execution" impact classes: the runtime pallet's `create_promotion_extrinsic` re-verification treats a cross-chain-replayed signature as valid on-chain consent.

### Likelihood Explanation
No privileged access, malicious node/validator/relayer, or leaked keys are required — only an ordinary user's own signature, or one they can observe from any public HOP submission, replayed against a second HOP deployment. The bug is deterministic and always exploitable whenever two HOP-enabled chains exist with overlapping account space and overlapping timestamp tolerance, which is the expected operating condition for any Substrate-based multi-chain ecosystem (e.g., staging vs. production, or sibling parachains).

### Recommendation
Fold a chain-specific domain separator into all three signing payloads — at minimum the genesis hash (`frame_system::BlockHash::<T>::get(0)`), and ideally also the spec version — before hashing, analogous to the existing `chainId`/`verifyingContract` binding already correctly implemented in the unrelated EIP-712 permit pallet (`substrate/frame/assets/precompiles/src/permit.rs`). E.g.:

```
blake2_256(context || genesis_hash || spec_version || hash [|| submit_timestamp])
```

The genesis hash/spec version must be supplied by the node (already has access via `client.info()`/runtime metadata) and re-derived identically by the runtime pallet during `create_promotion_extrinsic` verification.

### Proof of Concept
1. On chain A, an account `Alice` calls `hop_submit(data, recipients, signature_A, signer, ts)` where `signature_A = Sign(blake2_256(HOP_SUBMIT_CONTEXT || blake2_256(data) || ts.to_le_bytes()))` [7](#0-6) .
2. An observer captures `(data, signer, signature_A, ts)` from the public RPC call or from the resulting on-chain promotion extrinsic.
3. Within the runtime's timestamp tolerance window, the observer submits the identical `(data, signer, signature_A, ts)` to chain B's `hop_submit` RPC (a distinct HOP-enabled chain where `Alice`'s account is also authorized under `can_account_promote`).
4. Chain B's client recomputes the same `submit_signing_payload` (no chain binding), verification succeeds, and the pool/pallet accepts the submission and later promotes it on-chain as if `Alice` had consented on chain B — despite `Alice` never intending or authorizing this action on chain B.

### Citations

**File:** substrate/client/hop/src/types.rs (L298-329)
```rust
/// Domain-separator prefix for `hop_submit` signatures.
pub const HOP_SUBMIT_CONTEXT: &[u8] = b"hop-submit-v1:";

/// Domain-separator prefix for `hop_claim` signatures.
pub const HOP_CLAIM_CONTEXT: &[u8] = b"hop-claim-v1:";

/// Domain-separator prefix for `hop_ack` signatures.
pub const HOP_ACK_CONTEXT: &[u8] = b"hop-ack-v1:";

/// Compute the 32-byte payload that HOP recipients / submitters sign for a given
/// operation. This is `blake2_256(context || hash)` and ensures signatures from
/// one operation cannot be replayed in another.
pub fn signing_payload(context: &[u8], hash: &HopHash) -> [u8; 32] {
	let mut buf = Vec::with_capacity(context.len() + 32);
	buf.extend_from_slice(context);
	buf.extend_from_slice(hash.as_bytes());
	blake2_256(&buf)
}

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

**File:** substrate/client/hop/src/rpc.rs (L207-219)
```rust
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

**File:** substrate/primitives/hop/src/lib.rs (L47-57)
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
