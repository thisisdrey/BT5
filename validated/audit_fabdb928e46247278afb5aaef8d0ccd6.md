The evidence confirms `iter_path_from_bottom` is not reachable from transaction processing at all — it's only called from `core/store/src/merkle_proof.rs` (in `compute_past_block_proof_in_merkle_tree_of_later_block`, used by light-client proof RPCs) and internally within `core/primitives/src/merkle.rs` itself. It operates on `PartialMerkleTree.path`/`size` fields that are built incrementally via `insert()` [1](#0-0)  as blocks are produced by the chain, and loaded from trusted chain storage (`ChainStore::get_block_merkle_tree`) [2](#0-1) , not deserialized from an attacker-supplied `SignedTransaction` payload on a per-request basis.

### Analysis [3](#0-2)  `iter_path_from_bottom` iterates over `self.path`, a `Vec<MerkleHash>` whose length equals `self.size.count_ones()` when well-formed. This vector is never populated from network/transaction input directly by an untrusted length prefix under attacker control — it grows one element at a time via `PartialMerkleTree::insert` [1](#0-0) , which is called once per block by `update_and_save_block_merkle_tree` [2](#0-1)  — a chain-internal, block-production-controlled path, not something a `send_tx`/`broadcast_tx_commit` RPC request body can influence in a single call.

There is no evidence in the codebase of `PartialMerkleTree` being borsh-deserialized from an attacker-controlled `SignedTransaction`, action payload, or any RPC request body. The only consumers found are `core/store/src/merkle_proof.rs` (internal proof computation, reading previously stored, node-generated trees) and `neard/src/cli.rs`'s `VerifyProofSubCommand` (a local CLI tool, not an RPC entrypoint) [4](#0-3) . Neither is reachable from `broadcast_tx_commit`/`send_tx` handling a `SignedTransaction`.

The premise of the question — that a "collection length field" and "enum discriminant" embedded in a submitted transaction could reach `iter_path_from_bottom` and cause work proportional to an oversized declared length — has no basis in the actual data flow: the function's iteration bound (`self.path.iter()`) is determined by the honestly-maintained internal `Vec` length from prior `insert()` calls, and the loop body itself is O(1) per path element with no nested/recursive decoding, so even in scenarios where `PartialMerkleTree` were deserialized (e.g., from trusted store bytes), there's no "duplicate or out-of-range enum discriminant" mechanic (there's no enum at all in this struct — only `Vec<MerkleHash>` and `u64`) and no way to make the loop iterate more than the actual serialized length of the `path` vector, since standard Borsh vector deserialization allocates based on declared length but also fails immediately if the input buffer is shorter (Borsh's `Vec<T>` deserialization reads a `u32` length then reads exactly that many elements, erroring out early if bytes run out, rather than allocating a huge vector unconditionally without bound — and per-item allocation for large `MerkleHash` type fixed-size arrays is bounded by remaining bytes in near's `BorshDeserialize` implementations).

#No vulnerability found for this question.

### Citations

**File:** core/primitives/src/merkle.rs (L198-208)
```rust
    pub fn insert(&mut self, elem: MerkleHash) {
        let mut s = self.size;
        let mut node = elem;
        while s % 2 == 1 {
            let last_path_elem = self.path.pop().unwrap();
            node = combine_hash(&last_path_elem, &node);
            s /= 2;
        }
        self.path.push(node);
        self.size += 1;
    }
```

**File:** core/primitives/src/merkle.rs (L220-234)
```rust
    pub fn iter_path_from_bottom(&self, mut f: impl FnMut(MerkleHash, u64)) {
        let mut level = 0;
        let mut index = self.size;
        for node in self.path.iter().rev() {
            if index == 0 {
                // shouldn't happen
                return;
            }
            let trailing_zeros = index.trailing_zeros();
            level += trailing_zeros;
            index >>= trailing_zeros;
            index -= 1;
            f(*node, level as u64);
        }
    }
```

**File:** chain/chain/src/store/mod.rs (L1589-1600)
```rust
    fn update_and_save_block_merkle_tree(&mut self, header: &BlockHeader) -> Result<(), Error> {
        if header.is_genesis() {
            self.save_block_merkle_tree(*header.hash(), PartialMerkleTree::default());
        } else {
            let prev_hash = header.prev_hash();
            let old_merkle_tree = self.get_block_merkle_tree(prev_hash)?;
            let mut new_merkle_tree = PartialMerkleTree::clone(&old_merkle_tree);
            new_merkle_tree.insert(*prev_hash);
            self.save_block_merkle_tree(*header.hash(), new_merkle_tree);
        }
        Ok(())
    }
```

**File:** neard/src/cli.rs (L827-837)
```rust
impl VerifyProofSubCommand {
    /// Verifies light client transaction proof (result of the EXPERIMENTAL_light_client_proof RPC call).
    /// Returns the Hash and height of the block that transaction belongs to, and root of the light block merkle tree.
    pub fn run(self) -> ((CryptoHash, u64), CryptoHash) {
        let file = File::open(Path::new(self.json_file_path.as_str()))
            .with_context(|| "Could not open proof file.")
            .unwrap();
        let reader = BufReader::new(file);
        let light_client_rpc_response: Value =
            serde_json::from_reader(reader).with_context(|| "Failed to deserialize JSON.").unwrap();
        Self::verify_json(light_client_rpc_response).unwrap()
```
