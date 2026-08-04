## Finding Summary

There is a real weight-vs-cost mismatch in `submit_delivery_receipt`, rooted in the fact that the pallet charges a **fixed** benchmarked weight for an extrinsic whose actual proof-verification cost scales with an **unbounded**, attacker-controlled input.

### Finding Description

`submit_delivery_receipt` is charged a constant weight: [1](#0-0) 

This weight is derived from the benchmark, which uses a fixture with a tiny, hand-crafted proof: a `receipt_proof` of ~2 trie nodes and an `ancestry_proof.header_branch`/`execution_branch` of fixed length (13 and 4 elements respectively): [2](#0-1) [3](#0-2) 

However, the wire type carrying the proof places **no bound** on the size of `receipt_proof`: [4](#0-3) 

it is a plain `Vec<Vec<u8>>`, decoded directly from the signed extrinsic with no `BoundedVec`/`MaxEncodedLen` cap and no explicit length check performed by the pallet before calling into the verifier: [5](#0-4) 

`verify_receipt_inclusion` forwards the entire attacker-supplied `receipt_proof` slice to `verify_receipt_proof`, which builds a `Vec<Bytes>` from every node and hands it to `alloy_trie::proof::verify_proof`: [6](#0-5) 

The number and size of nodes in this vector are entirely attacker-controlled — a relayer/attacker can pad the proof with an arbitrarily large number of garbage byte blobs (bounded only by the outer block-length/extrinsic-size limits, not by anything in this pallet), forcing `alloy_trie`'s proof verification to process far more data (RLP decoding + hashing) than the benchmark ever exercised, while `submit_delivery_receipt`'s charged weight stays fixed at the benchmarked value.

By contrast, the beacon-side merkle checks used for `ancestry_proof.header_branch` and `execution_branch` are *not* vulnerable to the same amplification: `verify_merkle_branch` fails immediately if `branch.len() != depth`, so mismatched-length branches are rejected in O(1) before any hashing loop runs: [7](#0-6) 

So the only unbounded, uncapped amplification surface identified is `Proof.receipt_proof`.

### Impact Explanation

If `alloy_trie::proof::verify_proof` performs work (RLP decoding, hashing) proportional to the number/size of supplied nodes before determining the proof is invalid — rather than failing on the very first node mismatch — an attacker can submit `submit_delivery_receipt` extrinsics with maximal-size `receipt_proof` vectors that consume substantially more CPU time than the fixed weight charged for them. Because Substrate schedules blocks based on charged weight, not actual CPU time, this constitutes "public underpriced work" that can degrade block production time (validators spend real wall-clock time beyond what was budgeted for the block), a directly in-scope Immunefi impact category ("public underpriced work that degrades block production or stalls bridge processing").

### Likelihood Explanation

The extrinsic is fully public/unprivileged (`ensure_signed(origin)?` only) — any relayer key can call it, and `receipt_proof` content is entirely attacker-supplied with no bound in the type or the pallet logic. The main uncertainty is the internal behavior of the external `alloy_trie` crate's `verify_proof`: whether it truly does per-node work proportional to input size regardless of validity, or whether it fails fast on the very first non-matching node in the path (in which case the extra garbage nodes would be cheap to reject). I was not able to inspect the `alloy_trie` crate source in this repository/index to confirm which behavior applies, so this should be verified directly against the `alloy-trie` dependency before treating it as confirmed. Absent that confirmation, this is a structural weakness (unbounded input on a fixed-weight extrinsic) rather than a proven exploit.

### Recommendation

- Bound `Proof.receipt_proof` (e.g., convert to a `BoundedVec` with a `MaxEncodedLen`/max node count and max node size derived from the deepest realistic Ethereum receipts-trie proof) so that a decode-time limit prevents oversized proofs from ever reaching `Verifier::verify`.
- Alternatively/additionally, charge weight for `submit_delivery_receipt` as a function of the encoded/measured size of `event.proof.receipt_proof` (similar to how `on_process_message`/`commit` scale with measured proof size), so genuinely larger proofs are charged proportionally rather than at the fixed benchmarked constant.
- Confirm with the `alloy-trie` maintainers/source whether `verify_proof` fails fast on the first invalid/mismatched node, or processes the full node list regardless of validity, to determine the actual severity of the current gap.

### Proof of Concept

Not fully constructible without confirming `alloy_trie::proof::verify_proof`'s internal cost model; the described PoC would submit `submit_delivery_receipt` with a `receipt_proof` containing thousands of maximally-sized garbage byte vectors appended to (or replacing) the correct proof path, and measure actual execution time versus the ~68ms benchmarked weight in `WeightInfo::submit_delivery_receipt()`.

### Citations

**File:** bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs (L298-311)
```rust
		#[pallet::call_index(1)]
		#[pallet::weight(T::WeightInfo::submit_delivery_receipt())]
		pub fn submit_delivery_receipt(
			origin: OriginFor<T>,
			event: Box<EventProof>,
		) -> DispatchResult
		where
			<T as frame_system::Config>::AccountId: From<[u8; 32]>,
		{
			let relayer = ensure_signed(origin)?;

			// submit message to verifier for verification
			T::Verifier::verify(&event.event_log, &event.proof)
				.map_err(|e| Error::<T>::Verification(e))?;
```

**File:** bridges/snowbridge/pallets/outbound-queue-v2/src/benchmarking.rs (L153-179)
```rust
	#[benchmark]
	fn submit_delivery_receipt() -> Result<(), BenchmarkError> {
		let caller: T::AccountId = whitelisted_caller();

		let message = make_submit_delivery_receipt_message();

		T::Helper::initialize_storage(message.finalized_header, message.block_roots_root);

		let receipt = DeliveryReceipt::try_from(&message.event.event_log).unwrap();

		let order = PendingOrder {
			nonce: receipt.nonce,
			fee: 0,
			block_number: frame_system::Pallet::<T>::current_block_number(),
		};
		<PendingOrders<T>>::insert(receipt.nonce, order);

		#[block]
		{
			assert_ok!(OutboundQueue::<T>::submit_delivery_receipt(
				RawOrigin::Signed(caller.clone()).into(),
				Box::new(message.event),
			));
		}

		Ok(())
	}
```

**File:** cumulus/parachains/runtimes/bridge-hubs/bridge-hub-westend/src/weights/snowbridge_pallet_outbound_queue_v2.rs (L121-138)
```rust
	/// Storage: `EthereumBeaconClient::LatestFinalizedBlockRoot` (r:1 w:0)
	/// Proof: `EthereumBeaconClient::LatestFinalizedBlockRoot` (`max_values`: Some(1), `max_size`: Some(32), added: 527, mode: `MaxEncodedLen`)
	/// Storage: `EthereumBeaconClient::FinalizedBeaconState` (r:1 w:0)
	/// Proof: `EthereumBeaconClient::FinalizedBeaconState` (`max_values`: None, `max_size`: Some(72), added: 2547, mode: `MaxEncodedLen`)
	/// Storage: UNKNOWN KEY `0xaed97c7854d601808b98ae43079dafb3` (r:1 w:0)
	/// Proof: UNKNOWN KEY `0xaed97c7854d601808b98ae43079dafb3` (r:1 w:0)
	/// Storage: `EthereumOutboundQueueV2::PendingOrders` (r:1 w:1)
	/// Proof: `EthereumOutboundQueueV2::PendingOrders` (`max_values`: None, `max_size`: Some(36), added: 2511, mode: `MaxEncodedLen`)
	fn submit_delivery_receipt() -> Weight {
		// Proof Size summary in bytes:
		//  Measured:  `320`
		//  Estimated: `3785`
		// Minimum execution time: 67_000_000 picoseconds.
		Weight::from_parts(68_000_000, 0)
			.saturating_add(Weight::from_parts(0, 3785))
			.saturating_add(T::DbWeight::get().reads(4))
			.saturating_add(T::DbWeight::get().writes(1))
	}
```

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

**File:** bridges/snowbridge/primitives/beacon/src/merkle_proof.rs (L8-21)
```rust
pub fn verify_merkle_branch(
	leaf: H256,
	branch: &[H256],
	index: usize,
	depth: usize,
	root: H256,
) -> bool {
	// verify the proof length
	if branch.len() != depth {
		return false;
	}
	// verify the computed merkle root
	root == compute_merkle_root(leaf, branch, index)
}
```
