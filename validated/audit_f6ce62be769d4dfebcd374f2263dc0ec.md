## Finding: Unbounded/unbounded-weight receipt-proof array in Snowbridge inbound queue `submit`

### Title
Unbounded Ethereum receipt-proof vector processed by public `submit` extrinsic without a length bound or weight scaling - (File: `bridges/snowbridge/primitives/verification/src/lib.rs`, `bridges/snowbridge/pallets/inbound-queue/src/lib.rs`, `bridges/snowbridge/pallets/inbound-queue-v2/src/lib.rs`)

### Summary
The external report's core invariant break is: a proof-verification loop iterates over an attacker-supplied list of proof elements with no upper bound on its length, letting a relayer/attacker submit an oversized proof and cause unbounded work relative to the weight charged. The same pattern exists in Snowbridge's Ethereum inbound message path: the `Proof.receipt_proof` field is an unbounded `Vec<Vec<u8>>` decoded straight from the public `submit` extrinsic and fed into a trie-proof verification loop, while the extrinsic's declared weight is a fixed constant rather than a function of the proof size.

### Finding Description
`Proof` is defined as: [1](#0-0) 

`receipt_proof: Vec<Vec<u8>>` has no `BoundedVec` wrapper and no explicit length/size check anywhere before it is used. It flows in from the public, unprivileged extrinsic: [2](#0-1) 

and the analogous `inbound-queue-v2::submit`, both weighted with a constant `T::WeightInfo::submit()` that does not scale with `event.encode().len()` or `proof.receipt_proof.len()`.

The proof is passed to `Verifier::verify` → `Self::verify_receipt_inclusion`, which calls: [3](#0-2) 

and ultimately into `verify_receipt_proof`, which loops over every node in `proof` (the caller-supplied `receipt_proof` vector) via `alloy_trie::proof::verify_proof`: [4](#0-3) 

This mirrors exactly the reported bug class: a loop over a caller-controlled proof array (`for i = 116; i <= _operatorData.length; i += 32 ... proof[index] = temp`) with no length bound enforced before the loop runs — except here it's `alloy_trie`'s node-by-node trie traversal over `receipt_proof: Vec<Vec<u8>>`, each element itself an unbounded `Vec<u8>`.

### Impact Explanation
Because `receipt_proof` has no maximum-length constraint and the extrinsic's benchmarked weight is fixed rather than proportional to the number/size of proof nodes supplied, an attacker can submit a `submit(event)` transaction with an arbitrarily large `receipt_proof` (many large byte vectors) that is fully SCALE-decoded and then walked through the trie verification loop, consuming CPU/PoV disproportionate to the weight charged. This is the "public underpriced work that degrades block production or stalls bridge processing" impact category called out in the gate: it lets an unprivileged relayer submit oversized, underpriced work into block execution on BridgeHub, degrading throughput or exhausting execution time for a fixed weight budget.

### Likelihood Explanation
`submit` is a fully public extrinsic requiring only a signed origin — no governance, no malicious relayer/validator collusion, and no off-chain trust assumption is needed beyond an attacker being an ordinary signed account able to submit extrinsics. The only friction is transaction size/PoV limits imposed by the outer node (block length limits), but there is no explicit in-pallet check rejecting an oversized `receipt_proof` before it is decoded and processed, and the call's weight is not derived from its size, so the fee/weight accounting does not reflect the actual execution cost.

### Recommendation
- Bound `receipt_proof` (e.g., wrap in a `BoundedVec` with a fixed max node count and max node size derived from realistic Ethereum trie depth/branch-node sizes) similar to how `substrate/utils/binary-merkle-tree`/`polkadot/node/primitives::Proof` enforce `MERKLE_PROOF_MAX_DEPTH`/`MERKLE_NODE_MAX_SIZE`.
- Reject proofs exceeding the bound with a dedicated error before calling into `verify_receipt_proof`.
- Make `submit`'s declared weight a function of the actual proof size (as is already done in `bridges/modules/messages` via `receive_messages_proof_weight(&**proof, ...)`), so PoV/compute cost tracks the untrusted input size.

### Proof of Concept
1. Craft an `EventProof` whose `proof.receipt_proof` contains a large number of maximal-size byte vectors (bounded only by the outer extrinsic/block size limits), rather than the small number of genuine trie nodes needed for a real Ethereum receipt proof.
2. Submit via `InboundQueue::submit(origin, Box::new(event))` (or v2's `submit`).
3. Observe that `verify_receipt_proof` iterates over the entire attacker-supplied `receipt_proof` list before failing verification, while the call was charged the fixed `T::WeightInfo::submit()` weight, independent of the size of `receipt_proof` actually supplied — demonstrating underpriced/uncapped work relative to the declared weight.

Note: I could not find any `BoundedVec`, explicit `ensure!(proof.receipt_proof.len() <= MAX...)` check, or size-based weight formula for `submit()` in either `inbound-queue` or `inbound-queue-v2` pallets in this repo snapshot; if such a check exists elsewhere in code the indexer did not surface, that would invalidate this finding, so a Devin session with full repo access should double-check `weights.rs` and any `benchmarking.rs` derived weight formulas for `submit` to confirm size-dependence before treating this as confirmed unbounded.

### Citations

**File:** bridges/snowbridge/primitives/verification/src/lib.rs (L56-62)
```rust
#[derive(Clone, Encode, Decode, DecodeWithMemTracking, PartialEq, Debug, TypeInfo)]
pub struct Proof {
	// Proof values from receipts tree
	pub receipt_proof: Vec<Vec<u8>>,
	// Proof that an execution header was finalized by the beacon chain
	pub execution_proof: ExecutionProof,
}
```

**File:** bridges/snowbridge/pallets/inbound-queue/src/lib.rs (L235-243)
```rust
		#[pallet::call_index(0)]
		#[pallet::weight(T::WeightInfo::submit())]
		pub fn submit(origin: OriginFor<T>, event: EventProof) -> DispatchResult {
			let who = ensure_signed(origin)?;
			ensure!(!Self::operating_mode().is_halted(), Error::<T>::Halted);

			// submit message to verifier for verification
			T::Verifier::verify(&event.event_log, &event.proof)
				.map_err(|e| Error::<T>::Verification(e))?;
```

**File:** bridges/snowbridge/pallets/ethereum-client/src/impls.rs (L21-41)
```rust
	fn verify(event_log: &Log, proof: &Proof) -> Result<(), VerificationError> {
		// Refuse to verify any Ethereum-side proof while the beacon light client is halted.
		// Governance halts the light client when it suspects a compromise (e.g. sync committee
		// takeover), at which point any signed headers/receipts must be treated as untrusted.
		// Covers every Verifier consumer, including `inbound_queue_v2::submit` and
		// `outbound_queue_v2::submit_delivery_receipt` (which would otherwise still drain
		// pending relayer rewards while the bridge is halted).
		ensure!(!Self::operating_mode().is_halted(), VerificationError::Halted);

		Self::verify_execution_proof(&proof.execution_proof)
			.map_err(|e| InvalidExecutionProof(e.into()))?;

		Self::verify_receipt_inclusion(
			proof.execution_proof.execution_header.receipts_root(),
			event_log.tx_index,
			&proof.receipt_proof,
			event_log,
		)?;

		Ok(())
	}
```

**File:** bridges/snowbridge/primitives/verification/src/receipt.rs (L13-36)
```rust
pub fn verify_receipt_proof(
	receipts_root: H256,
	tx_index: u64,
	proof: &[Vec<u8>],
) -> Option<ReceiptEnvelope> {
	let key = receipt_trie_key(tx_index);
	let root = B256::from_slice(receipts_root.as_bytes());
	let proof_nodes: Vec<Bytes> = proof.iter().map(|node| Bytes::copy_from_slice(node)).collect();

	// Call verify_proof with None to extract the value from an inclusion proof. For inclusion
	// proofs, alloy_trie returns ValueMismatch with the extracted value in `got`. The proof is
	// already cryptographically verified during this traversal.
	let value = match verify_proof(root, key, None, proof_nodes.iter()) {
		Ok(()) => return None, // Exclusion proof - key does not exist
		Err(ProofVerificationError::ValueMismatch { path, got: Some(v), expected: None })
			if path == key =>
		{
			v.to_vec()
		},
		Err(_) => return None,
	};

	ReceiptEnvelope::decode(&mut value.as_slice()).ok()
}
```
