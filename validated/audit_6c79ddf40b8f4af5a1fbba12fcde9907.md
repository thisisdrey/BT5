### Title
Missing `chain_id` in `RotationProofChallenge` Enables Cross-Chain Replay of Key Rotation Signatures — (`aptos-move/framework/aptos-framework/sources/account/account.move`)

---

### Summary

`RotationProofChallenge` does not include a `chain_id` field. A valid rotation proof signature captured from one Aptos network (e.g., testnet, chain_id=2) can be replayed verbatim on a different network (e.g., mainnet, chain_id=1) to rotate the authentication key of the same account address without the account owner's consent, provided the account has the same `sequence_number` and `authentication_key` on both chains.

---

### Finding Description

The `RotationProofChallenge` struct is defined as:

```move
struct RotationProofChallenge has copy, drop {
    sequence_number: u64,
    originator: address,
    current_auth_key: address,
    new_public_key: vector<u8>,
}
``` [1](#0-0) 

There is no `chain_id` field. The inline comment claims replay is prevented by the "TXN's unique sequence number," but sequence numbers are per-account counters that are independently identical across chains for accounts with the same history. [2](#0-1) 

The `rotate_authentication_key` entry function constructs the challenge from on-chain state and verifies the caller-supplied signature bytes against it:

```move
let challenge = RotationProofChallenge {
    sequence_number: account_resource.sequence_number,
    originator: addr,
    current_auth_key: curr_auth_key_as_address,
    new_public_key: to_public_key_bytes,
};
assert_valid_rotation_proof_signature_and_get_auth_key(from_scheme, from_public_key_bytes, cap_rotate_key, &challenge);
``` [3](#0-2) 

`assert_valid_rotation_proof_signature_and_get_auth_key` calls `ed25519::signature_verify_strict_t` directly on the challenge struct with no additional domain separation: [4](#0-3) 

The transaction-level `chain_id` check only validates the outer transaction envelope. The `cap_rotate_key` and `cap_update_table` bytes are opaque parameters — they are not re-bound to the executing chain.

**Contrast with the fixed sibling struct:** `RotationCapabilityOfferProofChallengeV2` was explicitly updated to include `chain_id` precisely to prevent this class of attack:

```move
struct RotationCapabilityOfferProofChallengeV2 has drop {
    chain_id: u8,
    sequence_number: u64,
    source_address: address,
    recipient_address: address,
}
``` [5](#0-4) 

`RotationProofChallenge` was never given the same treatment.

---

### Impact Explanation

An attacker who observes a legitimate `rotate_authentication_key` transaction on chain X (e.g., testnet) can extract the `cap_rotate_key` and `cap_update_table` signature bytes and replay them on chain Y (e.g., mainnet) by submitting a new transaction (with chain_id=Y in the envelope) calling `rotate_authentication_key` with the same parameters. If account A on chain Y has the same `sequence_number` and `authentication_key` as it did on chain X at the time of signing, the challenge reconstructed on chain Y is byte-for-byte identical, the signatures verify, and account A's authentication key on chain Y is rotated to the attacker-chosen key `P`. The account owner loses control of their account on chain Y.

---

### Likelihood Explanation

The precondition — matching `sequence_number` and `authentication_key` across two chains — is realistic for:
- Newly created accounts (sequence_number=0, auth_key derived from the same keypair) that exist on both mainnet and testnet.
- Accounts that have performed the same sequence of operations on both networks (common for developers and power users).

The attacker only needs to monitor the public chain for a rotation transaction and submit a replay transaction before the victim's sequence number advances on chain Y.

---

### Recommendation

Add `chain_id: u8` to `RotationProofChallenge`, populated via `chain_id::get()` at construction time in `rotate_authentication_key` and `rotate_authentication_key_with_rotation_cap`, mirroring the fix already applied to `RotationCapabilityOfferProofChallengeV2`. [6](#0-5) 

---

### Proof of Concept

```rust
// Pseudocode Rust integration test
// Step 1: Sign RotationProofChallenge on chain_id=2 (testnet harness)
let challenge = RotationProofChallenge {
    account_address: CORE_CODE_ADDRESS,
    module_name: "account".to_string(),
    struct_name: "RotationProofChallenge".to_string(),
    sequence_number: 0,
    originator: alice_addr,
    current_auth_key: alice_addr,  // fresh account, auth_key == address
    new_public_key: attacker_pk.to_bytes().to_vec(),
};
let msg = bcs::to_bytes(&challenge).unwrap();
let cap_rotate = alice_sk.sign_arbitrary_message(&msg);
let cap_update = attacker_sk.sign_arbitrary_message(&msg);

// Step 2: Submit to chain_id=1 (mainnet harness) — same account, same seq=0, same auth_key
// The transaction envelope carries chain_id=1 (passes tx-level check).
// The embedded cap_rotate/cap_update bytes are identical — no chain binding.
mainnet_harness.run_transaction_payload(
    &alice,  // same address exists on mainnet with seq=0
    aptos_stdlib::account_rotate_authentication_key(
        0, alice_pk.to_bytes(), 0, attacker_pk.to_bytes(),
        cap_rotate.to_bytes().to_vec(),
        cap_update.to_bytes().to_vec(),
    )
);
// Assert: rotation SUCCEEDS on mainnet — alice's key is now attacker_pk
// Expected: rotation should FAIL with chain binding error
```

The Rust struct used by the CLI confirms no `chain_id` field exists in the serialized challenge: [7](#0-6)

### Citations

**File:** aptos-move/framework/aptos-framework/sources/account/account.move (L120-121)
```text
    /// knowledge of this new public key's associated secret key. These two signatures cannot be replayed in another
    /// context because they include the TXN's unique sequence number.
```

**File:** aptos-move/framework/aptos-framework/sources/account/account.move (L122-131)
```text
    struct RotationProofChallenge has copy, drop {
        sequence_number: u64,
        // the sequence number of the account whose key is being rotated
        originator: address,
        // the address of the account whose key is being rotated
        current_auth_key: address,
        // the current authentication key of the account whose key is being rotated
        new_public_key: vector<u8>,
        // the new public key that the account owner wants to rotate to
    }
```

**File:** aptos-move/framework/aptos-framework/sources/account/account.move (L148-153)
```text
    struct RotationCapabilityOfferProofChallengeV2 has drop {
        chain_id: u8,
        sequence_number: u64,
        source_address: address,
        recipient_address: address,
    }
```

**File:** aptos-move/framework/aptos-framework/sources/account/account.move (L688-701)
```text
        let challenge = RotationProofChallenge {
            sequence_number: account_resource.sequence_number,
            originator: addr,
            current_auth_key: curr_auth_key_as_address,
            new_public_key: to_public_key_bytes,
        };

        // Assert the challenges signed by the current and new keys are valid
        assert_valid_rotation_proof_signature_and_get_auth_key(
            from_scheme,
            from_public_key_bytes,
            cap_rotate_key,
            &challenge
        );
```

**File:** aptos-move/framework/aptos-framework/sources/account/account.move (L824-829)
```text
        let proof_challenge = RotationCapabilityOfferProofChallengeV2 {
            chain_id: chain_id::get(),
            sequence_number: account_resource.sequence_number,
            source_address: addr,
            recipient_address,
        };
```

**File:** aptos-move/framework/aptos-framework/sources/account/account.move (L1056-1069)
```text
    fun assert_valid_rotation_proof_signature_and_get_auth_key(
        scheme: u8,
        public_key_bytes: vector<u8>,
        signature: vector<u8>,
        challenge: &RotationProofChallenge
    ): vector<u8> {
        if (scheme == ED25519_SCHEME) {
            let pk = ed25519::new_unvalidated_public_key_from_bytes(public_key_bytes);
            let sig = ed25519::new_signature_from_bytes(signature);
            assert!(
                ed25519::signature_verify_strict_t(&sig, &pk, *challenge),
                std::error::invalid_argument(EINVALID_PROOF_OF_KNOWLEDGE)
            );
            ed25519::unvalidated_public_key_to_authentication_key(&pk)
```

**File:** types/src/account_config/resources/challenge.rs (L13-24)
```rust
pub struct RotationProofChallenge {
    // Should be `CORE_CODE_ADDRESS`
    pub account_address: AccountAddress,
    // Should be `account`
    pub module_name: String,
    // Should be `RotationProofChallenge`
    pub struct_name: String,
    pub sequence_number: u64,
    pub originator: AccountAddress,
    pub current_auth_key: AccountAddress,
    pub new_public_key: Vec<u8>,
}
```
