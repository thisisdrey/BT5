### Title
Missing `chain_id` in `SignerCapabilityOfferProofChallengeV2` Enables Cross-Chain Replay to Hijack Signer Capability — (`aptos-move/framework/aptos-framework/sources/account/account.move`)

---

### Summary

`SignerCapabilityOfferProofChallengeV2`, the proof-of-knowledge struct signed by an account owner when delegating signer capability via `offer_signer_capability`, omits the `chain_id` field. A signature produced on any Aptos network (e.g., testnet, devnet) is therefore valid on every other Aptos network where the victim's account has the same sequence number. An attacker who observes the victim's off-chain or testnet signature can replay it on mainnet to install themselves (or a colluding address) as the authorized signer of the victim's account, then drain all assets.

---

### Finding Description

`RotationCapabilityOfferProofChallengeV2` was explicitly upgraded to include `chain_id` to prevent cross-chain replay:

```move
// account.move:145-153
/// This V2 struct adds the `chain_id` and `source_address` to the challenge message,
/// which prevents replaying the challenge message.
struct RotationCapabilityOfferProofChallengeV2 has drop {
    chain_id: u8,
    sequence_number: u64,
    source_address: address,
    recipient_address: address,
}
``` [1](#0-0) 

The sibling struct `SignerCapabilityOfferProofChallengeV2` was **not** given the same treatment:

```move
// account.move:155-159
struct SignerCapabilityOfferProofChallengeV2 has copy, drop {
    sequence_number: u64,
    source_address: address,
    recipient_address: address,
}
``` [2](#0-1) 

`offer_signer_capability` constructs and verifies this struct without ever binding it to the current chain:

```move
// account.move:966-972
let proof_challenge = SignerCapabilityOfferProofChallengeV2 {
    sequence_number: get_sequence_number(source_address),
    source_address,
    recipient_address,
};
verify_signed_message(
    source_address, account_scheme, account_public_key_bytes, signer_capability_sig_bytes, proof_challenge);
``` [3](#0-2) 

`verify_signed_message` performs only a cryptographic check against the struct bytes; it adds no chain binding of its own: [4](#0-3) 

The e2e test for `offer_signer_capability` confirms the struct used in practice also omits `chain_id`:

```rust
// offer_signer_capability.rs:15-22
struct SignerCapabilityOfferProofChallengeV2 {
    account_address: AccountAddress,
    module_name: String,
    struct_name: String,
    sequence_number: u64,
    source_address: AccountAddress,
    recipient_address: AccountAddress,
}
``` [5](#0-4) 

Compare with the rotation-capability test, which correctly includes `chain_id`: [6](#0-5) 

---

### Impact Explanation

`create_authorized_signer` grants the holder of a signer capability the ability to produce a `signer` for the victim's address and execute arbitrary Move entry functions on their behalf: [7](#0-6) 

With an unauthorized signer capability installed, the attacker can transfer all APT, fungible assets, and token objects owned by the victim — constituting direct theft of user-controlled on-chain assets.

---

### Likelihood Explanation

- Accounts start at `sequence_number = 0` on every Aptos network, so a signature produced on testnet at sequence 0 is immediately replayable on mainnet at sequence 0.
- Testnet transactions are publicly visible; any `offer_signer_capability` call leaks the raw `signer_capability_sig_bytes`.
- The attack requires only submitting one mainnet transaction with the replayed bytes — no privileged access, no special tooling.
- The same key pair is commonly used across Aptos networks by developers and power users.

---

### Recommendation

Add `chain_id: u8` to `SignerCapabilityOfferProofChallengeV2`, exactly as was done for `RotationCapabilityOfferProofChallengeV2`, and populate it with `chain_id::get()` inside `offer_signer_capability`:

```move
struct SignerCapabilityOfferProofChallengeV2 has copy, drop {
    chain_id: u8,          // <-- add this
    sequence_number: u64,
    source_address: address,
    recipient_address: address,
}

// in offer_signer_capability:
let proof_challenge = SignerCapabilityOfferProofChallengeV2 {
    chain_id: chain_id::get(),   // <-- add this
    sequence_number: get_sequence_number(source_address),
    source_address,
    recipient_address,
};
```

Because this changes the signed message format, a migration note or version bump is needed for any existing off-chain tooling that constructs this struct.

---

### Proof of Concept

1. **Testnet**: Alice (key pair `(sk_A, pk_A)`, address `addr_A`, sequence number 0) calls `offer_signer_capability` on testnet, delegating to attacker address `addr_X`. The transaction is broadcast and the `signer_capability_sig_bytes` value `σ` is extracted from the submitted transaction.

2. **Mainnet**: Alice has the same key pair and address on mainnet, also at sequence number 0 (fresh account). The attacker submits the following mainnet transaction:

   ```
   0x1::account::offer_signer_capability(
       signer_capability_sig_bytes = σ,   // replayed from testnet
       account_scheme = 0,                // ED25519
       account_public_key_bytes = pk_A,
       recipient_address = addr_X,
   )
   ```

3. `offer_signer_capability` reconstructs `SignerCapabilityOfferProofChallengeV2 { sequence_number: 0, source_address: addr_A, recipient_address: addr_X }` — identical bytes to what Alice signed on testnet — and `verify_signed_message` accepts `σ`.

4. `addr_X` is now recorded as the authorized signer for `addr_A` on mainnet. The attacker calls `create_authorized_signer` and transfers all of Alice's mainnet APT to themselves.

### Citations

**File:** aptos-move/framework/aptos-framework/sources/account/account.move (L145-153)
```text
    /// This struct stores the challenge message that should be signed by the source account, when the source account
    /// is delegating its rotation capability to the `recipient_address`.
    /// This V2 struct adds the `chain_id` and `source_address` to the challenge message, which prevents replaying the challenge message.
    struct RotationCapabilityOfferProofChallengeV2 has drop {
        chain_id: u8,
        sequence_number: u64,
        source_address: address,
        recipient_address: address,
    }
```

**File:** aptos-move/framework/aptos-framework/sources/account/account.move (L155-159)
```text
    struct SignerCapabilityOfferProofChallengeV2 has copy, drop {
        sequence_number: u64,
        source_address: address,
        recipient_address: address,
    }
```

**File:** aptos-move/framework/aptos-framework/sources/account/account.move (L966-972)
```text
        let proof_challenge = SignerCapabilityOfferProofChallengeV2 {
            sequence_number: get_sequence_number(source_address),
            source_address,
            recipient_address,
        };
        verify_signed_message(
            source_address, account_scheme, account_public_key_bytes, signer_capability_sig_bytes, proof_challenge);
```

**File:** aptos-move/framework/aptos-framework/sources/account/account.move (L1029-1040)
```text
    public fun create_authorized_signer(account: &signer, offerer_address: address): signer acquires Account {
        assert_account_resource_with_error(offerer_address, ENO_SUCH_SIGNER_CAPABILITY);
        // Check if there's an existing signer capability offer from the offerer.
        let account_resource = &Account[offerer_address];
        let addr = signer::address_of(account);
        assert!(
            account_resource.signer_capability_offer.for.contains(&addr),
            error::not_found(ENO_SUCH_SIGNER_CAPABILITY)
        );

        create_signer(offerer_address)
    }
```

**File:** aptos-move/framework/aptos-framework/sources/account/account.move (L1282-1320)
```text
    public fun verify_signed_message<T: drop>(
        account: address,
        account_scheme: u8,
        account_public_key: vector<u8>,
        signed_message_bytes: vector<u8>,
        message: T,
    ) acquires Account {
        let auth_key = get_authentication_key(account);
        // Verify that the `SignerCapabilityOfferProofChallengeV2` has the right information and is signed by the account owner's key
        if (account_scheme == ED25519_SCHEME) {
            let pubkey = ed25519::new_unvalidated_public_key_from_bytes(account_public_key);
            let expected_auth_key = ed25519::unvalidated_public_key_to_authentication_key(&pubkey);
            assert!(
                auth_key == expected_auth_key,
                error::invalid_argument(EWRONG_CURRENT_PUBLIC_KEY),
            );

            let signer_capability_sig = ed25519::new_signature_from_bytes(signed_message_bytes);
            assert!(
                ed25519::signature_verify_strict_t(&signer_capability_sig, &pubkey, message),
                error::invalid_argument(EINVALID_PROOF_OF_KNOWLEDGE),
            );
        } else if (account_scheme == MULTI_ED25519_SCHEME) {
            let pubkey = multi_ed25519::new_unvalidated_public_key_from_bytes(account_public_key);
            let expected_auth_key = multi_ed25519::unvalidated_public_key_to_authentication_key(&pubkey);
            assert!(
                auth_key == expected_auth_key,
                error::invalid_argument(EWRONG_CURRENT_PUBLIC_KEY),
            );

            let signer_capability_sig = multi_ed25519::new_signature_from_bytes(signed_message_bytes);
            assert!(
                multi_ed25519::signature_verify_strict_t(&signer_capability_sig, &pubkey, message),
                error::invalid_argument(EINVALID_PROOF_OF_KNOWLEDGE),
            );
        } else {
            abort error::invalid_argument(EINVALID_SCHEME)
        };
    }
```

**File:** aptos-move/e2e-move-tests/src/tests/offer_signer_capability.rs (L15-22)
```rust
struct SignerCapabilityOfferProofChallengeV2 {
    account_address: AccountAddress,
    module_name: String,
    struct_name: String,
    sequence_number: u64,
    source_address: AccountAddress,
    recipient_address: AccountAddress,
}
```

**File:** aptos-move/e2e-move-tests/src/tests/offer_rotation_capability.rs (L17-25)
```rust
struct RotationCapabilityOfferProofChallengeV2 {
    account_address: AccountAddress,
    module_name: String,
    struct_name: String,
    chain_id: u8,
    sequence_number: u64,
    source_address: AccountAddress,
    recipient_address: AccountAddress,
}
```
