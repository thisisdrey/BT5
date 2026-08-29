[1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3)

### Citations

**File:** runtime/runtime/src/verifier.rs (L302-302)
```rust
    let effective_nonce = std::cmp::max(access_key.nonce, pending.max_nonce);
```

**File:** runtime/runtime/src/verifier.rs (L307-309)
```rust
    // saturating_sub is fine here: on the consensus path pending constraints
    // are always default (zero), so the subtraction is exact. On the RPC /
    // chunk-production path it is best-effort and does not affect consensus.
```

**File:** runtime/runtime/src/verifier.rs (L359-359)
```rust
        access_key_update: AccessKeyUpdate::Regular { nonce: tx_nonce, new_allowance },
```

**File:** runtime/runtime/src/verifier.rs (L416-416)
```rust
    let effective_nonce = std::cmp::max(current_nonce, pending.max_nonce);
```
