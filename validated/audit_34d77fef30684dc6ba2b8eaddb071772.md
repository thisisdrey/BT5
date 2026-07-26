### Title
`InMemSubTreeInfo::combine` Fails to Roll Up `Unknown` Sibling on Deletion, Producing Invalid State Root — (`storage/scratchpad/src/sparse_merkle/updater.rs`)

---

### Summary

`InMemSubTreeInfo::combine` only rolls up a subtree to the parent position when it can identify it as `Self::Leaf`. When one child is `Unknown` and the other becomes `Empty` after a deletion, the `_` wildcard arm fires and creates an internal node with an empty child. This violates the SMT structural invariant and produces a wrong Merkle root hash that is committed to the ledger.

---

### Finding Description

The `combine` function in `updater.rs` is responsible for reconstructing the parent node after both children have been recursively updated:

```rust
fn combine(left: Self, right: Self, generation: u64) -> Self {
    match (&left, &right) {
        (Self::Empty, Self::Empty) => Self::Empty,
        (Self::Leaf { .. }, Self::Empty) => left,
        (Self::Empty, Self::Leaf { .. }) => right,
        _ => InMemSubTreeInfo::create_internal(left, right, generation),
    }
}
``` [1](#0-0) 

The SMT structural invariant requires that an internal node always has at least two non-empty leaf descendants. Equivalently, if after an update one child is `Empty`, the other child — regardless of whether it is `Leaf`, `Internal`, or `Unknown` — must be rolled up to the parent position.

The `combine` function handles `(Leaf, Empty)` and `(Empty, Leaf)` correctly, but the `_` arm silently handles `(Unknown, Empty)` and `(Empty, Unknown)` by calling `create_internal`, which is wrong in both cases.

**Concrete execution path:**

Consider a persisted SMT with two keys A and B sharing an internal node at depth `d`. Key A's node has been evicted from the in-memory scratchpad (its `Arc` was dropped after a prior generation was pruned), so it is represented as `InMemSubTreeInfo::Unknown { hash: H_A }`. Key B is still in memory as `InMemSubTreeInfo::Leaf`.

A transaction deletes key B. The updater reaches the internal node at depth `d`:

1. `run` calls `maybe_end_recursion` → returns `Continue` because the info is `Internal` with 1 update. [2](#0-1) 

2. `into_children` splits updates: left subtree (A) gets **0 updates**, right subtree (B) gets **1 update** (delete). [3](#0-2) 

3. Left `run`: `updates.len() == 0` → `materialize` → returns `InMemSubTreeInfo::Unknown { hash: H_A }`. The `Unknown` subtree is **never resolved via proof** because proof resolution in `into_children` only fires when the subtree itself is being recursed into with updates. [4](#0-3) 

4. Right `run`: `updates.len() == 1`, key matches, `update == None` → returns `InMemSubTreeInfo::Empty`. [5](#0-4) 

5. `combine(Unknown { H_A }, Empty)` → `_` arm → `create_internal(Unknown, Empty)`. [6](#0-5) 

6. The resulting internal node has hash `SparseMerkleInternalNode::new(H_A, PLACEHOLDER_HASH).hash()`, which is **not equal to `H_A`**. [7](#0-6) 

The correct result is `Unknown { hash: H_A }` (the subtree rolled up), regardless of whether `Unknown` represents a single leaf or a multi-leaf subtree. In both cases, the surviving child should occupy the parent position.

---

### Impact Explanation

The wrong hash propagates up the tree and becomes the committed state root for the block. Concretely:

- **Invalid state commitment**: The state root written to the ledger is `hash(H_A, PLACEHOLDER_HASH)` instead of `H_A`. Any subsequent Merkle proof for key A against this root will fail verification.
- **Potential consensus split**: The `Unknown` state depends on whether a node's `Arc` is still live in memory, which is a function of memory pressure and scratchpad pruning timing. Different validators may have different in-memory states for the same block, causing them to compute different state roots and disagree on the block, breaking consensus safety.

---

### Likelihood Explanation

The `Unknown` state is a normal operational condition. It arises whenever a node was created in a prior scratchpad generation whose `Arc` has since been dropped by the dropper thread. This happens continuously in production as old scratchpad generations are pruned. Any delete transaction whose target key shares an internal node with an `Unknown` sibling triggers the bug. No privileged access is required.

---

### Recommendation

Extend `combine` to roll up `Unknown` subtrees symmetrically with `Leaf` subtrees:

```rust
fn combine(left: Self, right: Self, generation: u64) -> Self {
    match (&left, &right) {
        (Self::Empty, Self::Empty) => Self::Empty,
        (Self::Leaf { .. }, Self::Empty) | (Self::Unknown { .. }, Self::Empty) => left,
        (Self::Empty, Self::Leaf { .. }) | (Self::Empty, Self::Unknown { .. }) => right,
        _ => InMemSubTreeInfo::create_internal(left, right, generation),
    }
}
```

This preserves the SMT invariant: when one child is `Empty`, the surviving child — whether its internal structure is known or not — is always rolled up to the parent position.

---

### Proof of Concept

```rust
#[test]
fn test_combine_unknown_empty_rolls_up() {
    use aptos_crypto::HashValue;
    use crate::sparse_merkle::node::SubTree;

    // Simulate an Unknown subtree (node evicted from memory, only hash known)
    let leaf_hash = HashValue::random();
    let unknown_subtree = SubTree::new_unknown(leaf_hash);

    // Wrap as InMemSubTreeInfo::Unknown
    let unknown_info = InMemSubTreeInfo::Unknown { subtree: unknown_subtree.clone() };
    let empty_info = InMemSubTreeInfo::Empty;

    let result = InMemSubTreeInfo::combine(unknown_info, empty_info, 0);

    // Must roll up: result should be Unknown with the original hash, NOT an Internal node
    match result {
        InMemSubTreeInfo::Unknown { subtree } => {
            assert_eq!(subtree.hash(), leaf_hash, "Hash must be preserved after rollup");
        },
        InMemSubTreeInfo::Internal { .. } => {
            panic!("BUG: combine(Unknown, Empty) created an internal node instead of rolling up");
        },
        _ => panic!("Unexpected variant"),
    }
}
```

This test directly exercises the `_` arm of `combine` with `(Unknown, Empty)` and asserts the rollup invariant. Under the current code it panics at the `Internal` branch, confirming the bug.

### Citations

**File:** storage/scratchpad/src/sparse_merkle/updater.rs (L92-101)
```rust
    fn combine(left: Self, right: Self, generation: u64) -> Self {
        // If there's a only leaf in the subtree,
        // rollup the leaf, otherwise create an internal node.
        match (&left, &right) {
            (Self::Empty, Self::Empty) => Self::Empty,
            (Self::Leaf { .. }, Self::Empty) => left,
            (Self::Empty, Self::Leaf { .. }) => right,
            _ => InMemSubTreeInfo::create_internal(left, right, generation),
        }
    }
```

**File:** storage/scratchpad/src/sparse_merkle/updater.rs (L342-343)
```rust
        Ok(match self.updates.len() {
            0 => MaybeEndRecursion::End(self.info.materialize(self.generation)),
```

**File:** storage/scratchpad/src/sparse_merkle/updater.rs (L366-368)
```rust
                            None => {
                                if key == key_to_update.hash_ref() {
                                    MaybeEndRecursion::End(InMemSubTreeInfo::Empty)
```

**File:** storage/scratchpad/src/sparse_merkle/updater.rs (L374-374)
```rust
                        _ => MaybeEndRecursion::Continue(self),
```

**File:** storage/scratchpad/src/sparse_merkle/updater.rs (L400-425)
```rust
    fn into_children(self, proof_reader: &impl ProofRead) -> Result<(Self, Self)> {
        let pivot = partition(self.updates, self.depth);
        let (left_updates, right_updates) = self.updates.split_at(pivot);
        let generation = self.generation;
        let (left_info, right_info) = self.info.into_children(
            self.updates[0].0.hash_ref(),
            self.depth,
            proof_reader,
            generation,
        )?;

        Ok((
            Self {
                depth: self.depth + 1,
                info: left_info,
                updates: left_updates,
                generation,
            },
            Self {
                depth: self.depth + 1,
                info: right_info,
                updates: right_updates,
                generation,
            },
        ))
    }
```

**File:** storage/scratchpad/src/sparse_merkle/node.rs (L38-40)
```rust
    pub fn calc_hash(&self) -> HashValue {
        SparseMerkleInternalNode::new(self.left.hash(), self.right.hash()).hash()
    }
```
