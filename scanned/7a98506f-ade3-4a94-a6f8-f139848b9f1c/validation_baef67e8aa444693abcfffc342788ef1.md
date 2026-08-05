The permit precompile already binds `chainId` and `verifyingContract` into its domain separator, so that's not the vulnerable analog — it's actually the fixed version of the pattern the report warns against.

The real local analog is in the HOP (Hand-Off Protocol) node service: `hop_claim`/`hop_ack` signatures are only bound to a domain-context string and the content hash, with no per-instance/per-chain identifier, mirroring exactly the missing "tie signature to a specific contract/instance" flaw from the report.

### Title
HOP claim/ack signatures lack chain/pool-instance binding, enabling cross-pool signature replay - (File: substrate/client/hop/src/types.rs)

### Summary
`hop_claim` and `hop_ack` both authorize the caller by verifying a `MultiSignature` over `signing_payload(context, hash)`, where `signing_payload` is simply `blake2_256(context || hash)` [1](#0-0) . `hash` is the content-addressed `blake2_256(data)`, not a per-node/per-chain nonce, and `context` is just the fixed literal `"hop-claim-v1:"` or `"hop-ack-v1:"` [2](#0-1) . Nothing in the signed payload ties the signature to a specific `HopDataPool` instance, node, or chain (no genesis hash, chain ID, or pool/node identifier is mixed in).

### Finding Description
`HopDataPool::claim` and `HopDataPool::ack` recover the signer and check it against the recipient list purely from `find_recipient_idx`, which calls `signing_payload(context, hash)` and verifies the decoded `MultiSignature` against it [3](#0-2) . Because content addressing means `hash = blake2_256(data)` is identical for identical bytes regardless of which node/pool the data was submitted to, and because the signed message is only `context || hash`, the exact same claim (or ack) signature a recipient produces for one `HopDataPool` instance is valid for *any other* `HopDataPool` instance that happens to store the same bytes under the same hash — e.g., the same data broadcast to multiple parachains/testnets/forked chains running `--enable-hop`, or resubmitted after a chain reset. This is structurally the same defect as the WaveContract bug: the signature authorizes an action ("claim this data" / "ack this data") without binding to the specific instance (contract address in the Solidity case; pool/chain identity here).

By contrast, `hop_submit` was deliberately hardened against exactly this class of bug by binding a `submit_timestamp` into the payload (`submit_signing_payload`) specifically to prevent indefinite replay [4](#0-3) , and the domain-separated `HOP_SUBMIT/CLAIM/ACK_CONTEXT` prefixes explicitly guard against cross-operation replay (submit signature reused as ack, etc.) as shown by `test_claim_sig_rejected_on_ack` [5](#0-4) . However, no equivalent protection exists for cross-*instance* replay of claim/ack signatures — the exact concern the external report raises about missing `address(this)`-style binding.

### Impact Explanation
Under this repo's stated impact gate, this issue's practical severity is limited: HOP is an off-chain, ephemeral, per-node data-availability side channel, not on-chain balance/state; `ack`'s only effect is marking a recipient's `claimed` flag and deleting the local blob copy once all recipients ack [6](#0-5) . Replaying a claim/ack signature across pool instances doesn't move funds or corrupt consensus state, so it likely falls short of the "theft/unbacked mint/duplicate settlement/chain takedown" bar required by the impact gate. It is, at most, a data-availability integrity nuisance (an attacker who observes one instance's claim/ack signature can pre-emptively ack/download the recipient's message on a different node/chain running the identical data and recipient set, causing that node to prematurely purge or serve the payload).

### Likelihood Explanation
Exploitation requires the same content bytes and recipient key set to exist as a HOP entry on more than one independent pool instance (e.g., multiple chains or forks sharing the HOP feature and reusing signing keys/content), which is a narrow, non-default operational condition. Given HOP is a new, opt-in (`--enable-hop`) node-side p2p feature and not part of core consensus/state, and the exploitable condition needs cross-deployment content+key reuse, likelihood is low.

### Recommendation
Bind a pool/chain-specific identifier into `signing_payload` (e.g., genesis hash or a configured pool/chain ID) analogous to including `address(this)` in the WaveContract fix, so claim/ack signatures are scoped to the specific `HopDataPool`/chain instance and cannot be replayed elsewhere.

### Proof of Concept
1. Operator runs two independent chains/nodes (`Node A`, `Node B`), both with `--enable-hop`.
2. A sender submits identical `data` bytes and an identical recipient `MultiSigner` list to both `hop_submit` endpoints; since `hash = blake2_256(data)` is content-addressed, both pools index the entry under the same `hash`.
3. The intended recipient calls `hop_claim`/`hop_ack` against Node A with signature `sig = sign(blake2_256(HOP_CLAIM_CONTEXT || hash))` (or ack context) [7](#0-6) .
4. An observer who captures `sig` (e.g., from a public RPC/log) replays the identical `(hash, sig)` pair against Node B's `hop_claim`/`hop_ack`; `find_recipient_idx` verifies successfully because the payload never encoded which pool/chain it was intended for [8](#0-7) , letting the observer download/ack the data on Node B without ever holding the recipient's private key input beyond the captured signature.

Given the limited real-world impact under this program's fund/consensus-focused gate, this should be treated as a low-severity hardening item rather than a critical vulnerability — flagging the uncertainty explicitly since I could not fully verify how/whether multi-chain HOP deployments with shared recipient keys occur in practice in this codebase's deployment model.

### Citations

**File:** substrate/client/hop/src/types.rs (L298-305)
```rust
/// Domain-separator prefix for `hop_submit` signatures.
pub const HOP_SUBMIT_CONTEXT: &[u8] = b"hop-submit-v1:";

/// Domain-separator prefix for `hop_claim` signatures.
pub const HOP_CLAIM_CONTEXT: &[u8] = b"hop-claim-v1:";

/// Domain-separator prefix for `hop_ack` signatures.
pub const HOP_ACK_CONTEXT: &[u8] = b"hop-ack-v1:";
```

**File:** substrate/client/hop/src/types.rs (L307-315)
```rust
/// Compute the 32-byte payload that HOP recipients / submitters sign for a given
/// operation. This is `blake2_256(context || hash)` and ensures signatures from
/// one operation cannot be replayed in another.
pub fn signing_payload(context: &[u8], hash: &HopHash) -> [u8; 32] {
	let mut buf = Vec::with_capacity(context.len() + 32);
	buf.extend_from_slice(context);
	buf.extend_from_slice(hash.as_bytes());
	blake2_256(&buf)
}
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

**File:** substrate/client/hop/src/pool.rs (L612-674)
```rust
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

		// Phase 2: re-run the lookup against the current meta — the entry could
		// have been removed and re-submitted with a different recipient list since Phase 1.
		let mut index = self.index.lock();
		let meta = index.get_mut(hash).ok_or(HopError::NotFound)?;
		let idx = Self::find_recipient_idx(meta, hash, signature, HOP_ACK_CONTEXT)
			.map_err(|_| HopError::NotFound)?;

		if meta.recipients[idx].claimed {
			return Ok(());
		}

		meta.recipients[idx].claimed = true;

		// If all recipients have acked, remove the entry entirely.
		if meta.recipients.iter().all(|r| r.claimed) {
			let accounted = entry_accounted_size(meta.size, meta.recipients.len());
			let sender = meta.sender_id;
			index.remove(hash);
			self.current_size.fetch_sub(accounted, Ordering::Relaxed);
			self.release_user_quota(&sender, accounted);
			drop(index);

			// Delete files from disk (best-effort; orphans cleaned on restart).
			let _ = fs::remove_file(self.blob_path(hash));
			let _ = fs::remove_file(self.meta_path(hash));

			tracing::info!(
				target: "hop",
				hash = ?hex::encode(hash),
				"All recipients acked, data removed"
			);
		} else {
			let claimed_count = meta.recipients.iter().filter(|r| r.claimed).count();
			// Persist updated claimed state to disk.
			let meta_bytes = meta.encode();
			let meta_path = self.meta_path(hash);
			if let Err(e) = Self::write_atomic(&meta_path, &meta_bytes) {
				tracing::error!(target: "hop", hash = ?hex::encode(hash), error = %e, "Failed to persist ack state");
			}
			drop(index);

			tracing::debug!(
				target: "hop",
				hash = ?hex::encode(hash),
				claimed = claimed_count,
				"Recipient acked"
			);
		}

		Ok(())
	}
```

**File:** substrate/client/hop/src/pool.rs (L1151-1163)
```rust
	#[test]
	fn test_claim_sig_rejected_on_ack() {
		// Domain separation: a claim signature cannot be replayed as an ack.
		let (pool, _dir) = create_test_pool();
		let (pair, signer) = test_recipient();
		let hash = pool
			.insert(vec![1, 2, 3], bv(vec![signer]), SENDER_A, dummy_auth().0, dummy_auth().1, 0)
			.unwrap();

		let claim = sign_ed(&pair, HOP_CLAIM_CONTEXT, &hash);
		pool.claim(&hash, &claim).unwrap();
		assert!(matches!(pool.ack(&hash, &claim), Err(HopError::NotFound)));
	}
```

**File:** substrate/client/hop/src/rpc.rs (L380-390)
```rust
	fn claim_sig(pair: &ed25519::Pair, hash: &H256) -> Bytes {
		use crate::types::{signing_payload, HOP_CLAIM_CONTEXT};
		let payload = signing_payload(HOP_CLAIM_CONTEXT, hash);
		Bytes(MultiSignature::Ed25519(pair.sign(&payload)).encode())
	}

	fn ack_sig(pair: &ed25519::Pair, hash: &H256) -> Bytes {
		use crate::types::{signing_payload, HOP_ACK_CONTEXT};
		let payload = signing_payload(HOP_ACK_CONTEXT, hash);
		Bytes(MultiSignature::Ed25519(pair.sign(&payload)).encode())
	}
```
