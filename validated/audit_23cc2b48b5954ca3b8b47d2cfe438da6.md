No vulnerability found for this question.

The `finish` function in `core/store/src/trie/mem/flexible_data/encoding.rs` is a trivial encoder-finalization routine: it asserts that the writer position matches the pre-allocated arena slice length and returns the slice [1](#0-0) . It contains no hashing logic, no truncation, and no key-comparison logic that could cause code from one account to resolve to another's hash.

Contract code storage keys are based on `TrieKey::ContractCode { account_id }` [2](#0-1) , and code integrity is verified via `value_ref.value_hash()` being compared against the full `code_hash` at multiple call sites (e.g. in `record_contract_call` and `get_code_len`), not a truncated hash [3](#0-2) [4](#0-3) . There is no mechanism by which shared key prefixes, extension-node lengths, or branch-node values in the memtrie encoding module could cause a full code hash to be truncated or collided against another account's code hash. The premise of the question does not correspond to any real code path in this file.

### Citations

**File:** core/store/src/trie/mem/flexible_data/encoding.rs (L59-65)
```rust
    /// Finishes the encoding process and returns a pointer to the allocated
    /// memory. The caller is responsible for freeing the pointer later.
    pub fn finish(self) -> ArenaSliceMut<'a, A::MemoryMut> {
        assert_eq!(self.pos, self.data.len());
        self.data
    }
}
```

**File:** core/primitives/src/trie_key.rs (L180-183)
```rust
    /// Used to store `Vec<u8>` contract code for a given `AccountId`.
    ContractCode {
        account_id: AccountId,
    } = col::CONTRACT_CODE,
```

**File:** runtime/runtime/src/function_call.rs (L385-393)
```rust
    let contract_ref = state_update
        .trie
        .get_optimized_ref(&key, KeyLookupMode::MemOrFlatOrTrie, AccessOptions::NO_SIDE_EFFECTS)
        .or_else(|err| {
            if matches!(err, StorageError::MissingTrieValue(_)) { Ok(None) } else { Err(err) }
        })?;
    if contract_ref.is_some_and(|value_ref| value_ref.value_hash() == code_hash) {
        state_update.contract_storage().record_call(code_hash);
    }
```

**File:** core/store/src/trie/update.rs (L193-205)
```rust
        let key = TrieKey::ContractCode { account_id };
        let value_ptr =
            self.get_ref(&key, KeyLookupMode::MemOrFlatOrTrie, AccessOptions::DEFAULT)?;
        if let Some(value_ptr) = value_ptr {
            debug_assert_eq!(
                code_hash,
                value_ptr.value_hash(),
                "Code-hash in trie does not match code-hash in account"
            );
            Ok(Some(value_ptr.len() as usize))
        } else {
            Ok(None)
        }
```
