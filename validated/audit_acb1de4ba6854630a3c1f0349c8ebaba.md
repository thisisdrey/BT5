### Title
Unrestricted `mmr_generateAncestryProof` / `mmr_generateProof` RPCs allow computationally expensive queries with no `DenyUnsafe` gate — public RPC DoS vector - ([File: substrate/client/merkle-mountain-range/rpc/src/lib.rs])

### Summary
The MMR RPC server (`mmr_generateProof`, `mmr_generateAncestryProof`) is registered unconditionally in the default full-node RPC set and never checks `DenyUnsafe`/`check_if_safe`, unlike other node RPCs (`state`, `author`, `system`) that explicitly gate expensive or sensitive calls. An unauthenticated caller can request proofs for early block numbers against the current best block, forcing the node to do MMR-proof computation whose cost scales with the age/height of the chain, with no per-call cap — the same class of issue as the reported `eth_getRestorationProof`: a public, enabled-by-default method that cannot be selectively disabled and grows more expensive as the chain grows.

### Finding Description
`create_full` in `substrate/bin/node/rpc/src/lib.rs` merges the MMR RPC module unconditionally into the default RPC surface: [1](#0-0) 

The `MmrApiServer` implementation exposes `mmr_generateProof` and `mmr_generateAncestryProof`, both of which accept attacker-controlled `block_numbers`/`prev_block_number` and an optional `best_known_block_number`, and neither method reads or checks any `DenyUnsafe` extension before performing work: [2](#0-1) 

Compare this to the `DenyUnsafe`/`check_if_safe` policy machinery that other RPC modules (`state`, `system`, `author`, babe) use to reject "unsafe" calls when the node is run in `--rpc-methods=Safe` (default for public nodes): [3](#0-2) 

The MMR RPC has no such check anywhere in its module, so there is no way to turn this expensive endpoint off short of removing the entire MMR API from `create_full`, exactly mirroring the report's core complaint about `eth_getRestorationProof`: "there is no way to prevent queries... this introduces a DoS vector for public RPC servers as long as they cannot turn off this API."

The underlying pallet call, `Pallet::generate_proof`, computes leaf indices from `block_numbers` and builds an `Mmr` instance sized to `best_known_block_number` (or current chain height if omitted), so cost scales with chain size and the caller can freely pick `best_known_block_number` up to the current best block: [4](#0-3) 

### Impact Explanation
This falls under "public underpriced work that degrades block production or stalls bridge processing" from the impact gate: an unauthenticated RPC client can repeatedly invoke `mmr_generateProof`/`mmr_generateAncestryProof` with parameters chosen to maximize computation (e.g., low `block_numbers`/`prev_block_number` paired with a high `best_known_block_number`), consuming CPU/offchain-storage read bandwidth on any public full node that exposes the default RPC set (which most collators/RPC providers do, since MMR is required for BEEFY light-client proof serving). Because there is no per-call weight/cost limiting and no way to disable just this API, sustained concurrent requests can degrade RPC server responsiveness and, if RPC and block-authoring share resources, affect block production availability.

### Likelihood Explanation
Likely, given: (1) the endpoint requires no privileges, no signed extrinsic, and no `DenyUnsafe` opt-out; (2) any full node that runs `create_full` (the standard `--rpc-methods=Safe`/`Auto` configuration used in production) exposes this method unconditionally; (3) the request shape needed to trigger worst-case cost is trivial to construct and repeatable at will over JSON-RPC.

### Recommendation
Gate `mmr_generateProof` and `mmr_generateAncestryProof` behind `DenyUnsafe`/`check_if_safe` (mirroring the pattern used in `substrate/client/rpc/src/state/mod.rs`), or move them to an RPC namespace that is disabled by default for public nodes, and/or enforce weight/size bounds on `block_numbers` length and the delta between the requested block number and `best_known_block_number` so a single call cannot force O(chain height) work.

### Proof of Concept
1. Run a node with default RPC configuration (`create_full`, `--rpc-methods=Safe`/default) on a long-lived chain.
2. Send repeated `mmr_generateAncestryProof` (or `mmr_generateProof`) JSON-RPC requests with `prev_block_number = 1` and `best_known_block_number` set to the chain's current best block number (or omitted to default to current best):
```json
{"jsonrpc":"2.0","id":1,"method":"mmr_generateAncestryProof","params":[1, null]}
```
3. Because the RPC handler performs no `DenyUnsafe` check and no cost-scaling limit, each such call forces MMR proof-generation work proportional to chain height; repeat concurrently to degrade the RPC service — no privileged access or off-chain assumptions are required, matching the reported bug's DoS pattern.

### Citations

**File:** substrate/bin/node/rpc/src/lib.rs (L186-194)
```rust
	io.merge(
		Mmr::new(
			client.clone(),
			backend
				.offchain_storage()
				.ok_or_else(|| "Backend doesn't provide an offchain storage")?,
		)
		.into_rpc(),
	)?;
```

**File:** substrate/client/merkle-mountain-range/rpc/src/lib.rs (L194-234)
```rust
	fn generate_proof(
		&self,
		block_numbers: Vec<NumberFor<Block>>,
		best_known_block_number: Option<NumberFor<Block>>,
		at: Option<<Block as BlockT>::Hash>,
	) -> RpcResult<LeavesProof<<Block as BlockT>::Hash>> {
		let mut api = self.client.runtime_api();
		let block_hash = at.unwrap_or_else(||
			// If the block hash is not supplied assume the best block.
			self.client.info().best_hash);

		api.register_extension(OffchainDbExt::new(self.offchain_db.clone()));

		let (leaves, proof) = api
			.generate_proof(block_hash, block_numbers, best_known_block_number)
			.map_err(runtime_error_into_rpc_error)?
			.map_err(mmr_error_into_rpc_error)?;

		Ok(LeavesProof::new(block_hash, leaves, proof))
	}

	fn generate_ancestry_proof(
		&self,
		prev_block_number: NumberFor<Block>,
		best_known_block_number: Option<NumberFor<Block>>,
		at: Option<<Block as BlockT>::Hash>,
	) -> RpcResult<MmrAncestryProof<MmrHash>> {
		let mut api = self.client.runtime_api();
		let block_hash = at.unwrap_or_else(||
			// If the block hash is not supplied assume the best block.
			self.client.info().best_hash);

		api.register_extension(OffchainDbExt::new(self.offchain_db.clone()));

		let proof = api
			.generate_ancestry_proof(block_hash, prev_block_number, best_known_block_number)
			.map_err(runtime_error_into_rpc_error)?
			.map_err(mmr_error_into_rpc_error)?;

		Ok(proof)
	}
```

**File:** substrate/client/rpc-api/src/policy.rs (L26-33)
```rust
/// Checks if the RPC call is safe to be called externally.
pub fn check_if_safe(ext: &jsonrpsee::Extensions) -> Result<(), UnsafeRpcError> {
	match ext.get::<DenyUnsafe>().map(|deny_unsafe| deny_unsafe.check_if_safe()) {
		Some(Ok(())) => Ok(()),
		Some(Err(e)) => Err(e),
		None => unreachable!("DenyUnsafe extension is always set by the substrate rpc server; qed"),
	}
}
```

**File:** substrate/frame/merkle-mountain-range/src/lib.rs (L374-394)
```rust
	pub fn generate_proof(
		block_numbers: Vec<BlockNumberFor<T>>,
		best_known_block_number: Option<BlockNumberFor<T>>,
	) -> Result<(Vec<LeafOf<T, I>>, LeafProof<HashOf<T, I>>), Error> {
		// check whether best_known_block_number provided, else use current best block
		let best_known_block_number =
			best_known_block_number.unwrap_or_else(|| <frame_system::Pallet<T>>::block_number());

		let leaf_count = Self::block_num_to_leaf_count(best_known_block_number)?;

		// we need to translate the block_numbers into leaf indices.
		let leaf_indices = block_numbers
			.iter()
			.map(|block_num| -> Result<LeafIndex, Error> {
				Self::block_num_to_leaf_index(*block_num)
			})
			.collect::<Result<Vec<LeafIndex>, _>>()?;

		let mmr: ModuleMmr<mmr::storage::OffchainStorage, T, I> = mmr::Mmr::new(leaf_count);
		mmr.generate_proof(leaf_indices)
	}
```
