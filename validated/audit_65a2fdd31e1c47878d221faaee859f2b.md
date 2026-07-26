### Title
Missing `signatures_required > 0` Validation in `MultiKey` Construction Allows Zero-Threshold Multi-Sig Account Authentication Key - (File: `aptos-move/framework/aptos-stdlib/sources/cryptography/multi_key.move`)

### Summary

The Move `aptos_std::multi_key` module exposes two public functions that construct a `MultiKey` struct without validating that `signatures_required > 0`. A crafted `MultiKey` with `signatures_required = 0` can be committed as an account's on-chain authentication key via `account::rotate_authentication_key_from_public_key`. At the Rust authenticator layer, the threshold check `signatures.len() >= signatures_required` is trivially satisfied when `signatures_required = 0`, reducing the effective signing requirement to 1-of-n (the minimum enforced by the bitmap non-empty check). This breaks the intended k-of-n invariant for `MultiKey` accounts.

### Finding Description

**Root cause 1 — `new_multi_key_from_single_keys()` missing lower-bound check:** [1](#0-0) 

The only guard on `signatures_required` is `(signatures_required as u64) <= num_keys`. When `signatures_required = 0` and `num_keys >= 1`, the condition `0 <= num_keys` is always true, so the function silently constructs and returns a `MultiKey { signatures_required: 0 }`.

**Root cause 2 — `deserialize_multi_key()` / `new_public_key_from_bytes()` perform zero validation:** [2](#0-1) 

`deserialize_multi_key` reads `signatures_required` from the BCS stream and stores it directly with no bounds check. `new_public_key_from_bytes` only asserts that no trailing bytes remain; it never validates the deserialized threshold.

**Formal spec also omits the zero check:** [3](#0-2) 

The Move Prover spec lists `aborts_if (signatures_required as u64) > len(single_keys)` but has no `aborts_if signatures_required == 0`, so the invariant is not formally enforced.

**Exploitation path — `rotate_authentication_key_from_public_key`:** [4](#0-3) 

When `scheme == MULTI_KEY_SCHEME`, the function calls `multi_key::new_public_key_from_bytes(new_public_key_bytes).to_authentication_key()` and writes the result directly to the account's `authentication_key`. Because `new_public_key_from_bytes` performs no threshold validation, a caller can supply BCS bytes encoding a `MultiKey` with `signatures_required = 0` and have it accepted as a valid authentication key.

**Rust-side threshold check is trivially bypassed:** [5](#0-4) 

`to_single_key_authenticators` enforces `self.signatures.len() >= self.public_keys.signatures_required()`. With `signatures_required = 0`, the condition `n >= 0` is always true for any `n`. The only remaining guard is: [6](#0-5) 

This requires at least one bit set in the bitmap, meaning at least one signature must be present. The net effect is that a 0-of-n `MultiKey` behaves as a 1-of-n at runtime — any single key from the set can authorize a transaction, regardless of the intended threshold.

**Contrast with `MultiKey::new()` in Rust (the validated constructor):** [7](#0-6) 

The Rust-side `MultiKey::new()` correctly rejects `signatures_required == 0`, but this constructor is not called during BCS deserialization of an incoming transaction — the `#[derive(Deserialize)]` path bypasses it entirely.

### Impact Explanation

An account owner who calls `rotate_authentication_key_from_public_key` (or any other key-rotation entry that accepts raw `MULTI_KEY_SCHEME` bytes) with a crafted payload encoding `signatures_required = 0` will have their account's authentication key set to a `MultiKey` whose effective signing threshold is 1 instead of the intended k. Any single key listed in the `MultiKey` — including keys belonging to third parties if the owner included them — can then unilaterally authorize transactions, including asset transfers, module publishes, and governance votes. This breaks the k-of-n authorization invariant that `MultiKey` accounts are designed to enforce.

### Likelihood Explanation

The vulnerability requires the account owner to supply a crafted `new_public_key_bytes` value. A buggy SDK, a malicious dApp, or a user who manually constructs the byte payload could trigger this. The entry point `rotate_authentication_key_from_public_key` is an unprivileged `entry fun` callable by any account holder.

### Recommendation

1. Add `assert!(signatures_required > 0, error::invalid_argument(E_INVALID_MULTI_KEY_SIGNATURES_REQUIRED))` to `new_multi_key_from_single_keys()`.
2. Add the same check to `deserialize_multi_key()` (or add a post-deserialization validation step in `new_public_key_from_bytes()`).
3. Update the Move Prover spec to include `aborts_if signatures_required == 0`.

### Proof of Concept

```move
// Attacker (or buggy SDK) constructs BCS bytes for a MultiKey with
// 2 public keys but signatures_required = 0x00.
//
// BCS layout: [uleb128 vector length = 2] [key1 bytes] [key2 bytes] [0x00]
//
// Call as the account owner:
account::rotate_authentication_key_from_public_key(
    &account_signer,
    MULTI_KEY_SCHEME,          // scheme = 3
    crafted_multikey_bytes,    // encodes MultiKey { keys: [pk1, pk2], signatures_required: 0 }
);
// Account's authentication_key is now hash(crafted_multikey_bytes || 0x03).
//
// Later, attacker submits a transaction signed only by pk1 (1 signature).
// to_single_key_authenticators() checks:
//   last_set_bit().is_some()  -> true  (1 bit set)
//   signatures.len() >= 0     -> true  (1 >= 0)
// Transaction is accepted. The intended 2-of-2 threshold is bypassed.
```

### Citations

**File:** aptos-move/framework/aptos-stdlib/sources/cryptography/multi_key.move (L59-74)
```text
    public fun new_multi_key_from_single_keys(single_keys: vector<single_key::AnyPublicKey>, signatures_required: u8): MultiKey {
        let num_keys = single_keys.length();
        assert!(
            num_keys > 0,
            error::invalid_argument(E_INVALID_MULTI_KEY_NO_KEYS)
        );
        assert!(
            num_keys <= MAX_NUMBER_OF_PUBLIC_KEYS,
            error::invalid_argument(E_INVALID_MULTI_KEY_TOO_MANY_KEYS)
        );
        assert!(
            (signatures_required as u64) <= num_keys,
            error::invalid_argument(E_INVALID_MULTI_KEY_SIGNATURES_REQUIRED)
        );
        MultiKey { public_keys: single_keys, signatures_required }
    }
```

**File:** aptos-move/framework/aptos-stdlib/sources/cryptography/multi_key.move (L77-81)
```text
    public fun deserialize_multi_key(stream: &mut bcs_stream::BCSStream): MultiKey {
        let public_keys = bcs_stream::deserialize_vector(stream, |x| single_key::deserialize_any_public_key(x));
        let signatures_required = bcs_stream::deserialize_u8(stream);
        MultiKey { public_keys, signatures_required }
    }
```

**File:** aptos-move/framework/aptos-stdlib/sources/cryptography/multi_key.spec.move (L6-15)
```text
    spec new_multi_key_from_single_keys(
        single_keys: vector<single_key::AnyPublicKey>,
        signatures_required: u8
    ): MultiKey {
        pragma opaque;
        aborts_if len(single_keys) == 0;
        aborts_if len(single_keys) > MAX_NUMBER_OF_PUBLIC_KEYS;
        aborts_if (signatures_required as u64) > len(single_keys);
        ensures result == MultiKey { public_keys: single_keys, signatures_required };
    }
```

**File:** aptos-move/framework/aptos-framework/sources/account/account.move (L466-481)
```text
    entry fun rotate_authentication_key_from_public_key(account: &signer, scheme: u8, new_public_key_bytes: vector<u8>) acquires Account {
        let addr = signer::address_of(account);
        let account_resource = &Account[addr];
        let old_auth_key = account_resource.authentication_key;
        let new_auth_key;
        if (scheme == ED25519_SCHEME) {
            let from_pk = ed25519::new_unvalidated_public_key_from_bytes(new_public_key_bytes);
            new_auth_key = ed25519::unvalidated_public_key_to_authentication_key(&from_pk);
        } else if (scheme == MULTI_ED25519_SCHEME) {
            let from_pk = multi_ed25519::new_unvalidated_public_key_from_bytes(new_public_key_bytes);
            new_auth_key = multi_ed25519::unvalidated_public_key_to_authentication_key(&from_pk);
        } else if (scheme == SINGLE_KEY_SCHEME) {
            new_auth_key = single_key::new_public_key_from_bytes(new_public_key_bytes).to_authentication_key();
        } else if (scheme == MULTI_KEY_SCHEME) {
            new_auth_key = multi_key::new_public_key_from_bytes(new_public_key_bytes).to_authentication_key();
        } else {
```

**File:** types/src/transaction/authenticator.rs (L1168-1171)
```rust
        ensure!(
            self.signatures_bitmap.last_set_bit().is_some(),
            "There were no signatures set in the bitmap."
        );
```

**File:** types/src/transaction/authenticator.rs (L1185-1190)
```rust
        ensure!(
            self.signatures.len() >= self.public_keys.signatures_required() as usize,
            "Not enough signatures for verification, {} < {}.",
            self.signatures.len(),
            self.public_keys.signatures_required(),
        );
```

**File:** types/src/transaction/authenticator.rs (L1241-1245)
```rust
    pub fn new(public_keys: Vec<AnyPublicKey>, signatures_required: u8) -> Result<Self> {
        ensure!(
            signatures_required > 0,
            "The number of required signatures is 0."
        );
```
