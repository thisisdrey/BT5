### Title
Missing `chain_id` in `SignerCapabilityOfferProofChallengeV2` Enables Cross-Chain Replay of Signer-Capability Grants - (`aptos-move/framework/aptos-framework/sources/account/account.move`)

---

### Summary

`SignerCapabilityOfferProofChallengeV2` — the struct whose BCS serialization is signed to authorize `offer_signer_capability` — omits the `chain_id` field that its sibling struct `RotationCapabilityOfferProofChallengeV2` explicitly includes for cross-chain replay protection. A signature produced on one Aptos network (e.g., testnet) can be replayed verbatim on any other Aptos network (e.g., mainnet) whenever the signer's sequence number and account addresses coincide, granting an unauthorized party full signer capability over the victim's mainnet account.

---

### Finding Description

In `account.move`, two proof-challenge structs govern capability delegation:

```move
// RotationCapabilityOfferProofChallengeV2 — has chain_id
struct RotationCapabilityOfferProofChallengeV2 has drop {
    chain_id: u8,          // ← cross-chain binding
    sequence_number: u64,
    source_address: address,
    recipient_address: address,
}

// SignerCapabilityOfferProofChallengeV2 — chain_id ABSENT
struct SignerCapabilityOfferProofChallengeV2 has copy, drop {
    sequence_number: u64,
    source_address: address,
    recipient_address: address,  // ← no chain_id
}
``` [1](#0-0) 

The comment on `RotationCapabilityOfferProofChallengeV2` explicitly states the motivation for V2:

> "This V2 struct adds the `chain_id` and `source_address` to the challenge message, which prevents replaying the challenge message." [2](#0-1) 

`SignerCapabilityOfferProofChallengeV2` was never given the same treatment. The `offer_signer_capability` entry function constructs the challenge without `chain_id`:

```move
let proof_challenge = SignerCapabilityOfferProofChallengeV2 {
    sequence_number: get_sequence_number(source_address),
    source_address,
    recipient_address,
};
verify_signed_message(
    source_address, account_scheme, account_public_key_bytes,
    signer_capability_sig_bytes, proof_challenge);
``` [3](#0-2) 

The `verify_signed_message` path calls `ed25519::signature_verify_strict_t` (or its multi-ed25519 equivalent), which wraps the struct in a `SignedMessage<T>` containing `type_info` (module address + module name + struct name) before BCS-serializing and verifying. The `type_info` provides type-level domain separation but **not** chain-level domain separation — the same `type_info` is identical on every Aptos network. [4](#0-3) 

The e2e test in `offer_signer_capability.rs` confirms the Rust-side struct also omits `chain_id` (compare with `offer_rotation_capability.rs` which includes it): [5](#0-4) [6](#0-5) 

---

### Impact Explanation

`create_authorized_signer` returns a live `signer` for the offerer's account to any address that holds a recorded signer-capability offer:

```move
public fun create_authorized_signer(account: &signer, offerer_address: address): signer
``` [7](#0-6) 

A signer is the root authorization primitive in Move. Possessing one for Alice's address allows the attacker to transfer all APT and fungible assets, rotate Alice's authentication key (locking her out), publish modules under her address, and perform any other privileged operation. This constitutes **direct theft and permanent freezing of user-controlled on-chain assets**.

---

### Likelihood Explanation

The preconditions are:

1. **Victim signs on a non-mainnet network.** Any testnet, devnet, or staging deployment shares the same module address (`0x1`) and struct layout.
2. **Sequence numbers coincide.** A freshly created account starts at sequence number 0 on every network. Accounts that are used lightly on testnet (sequence number 0 or 1) are common.
3. **Attacker is the designated recipient.** The attacker must be the `recipient_address` in the original signature — i.e., the victim must have willingly signed a capability offer to the attacker on a test network, or the attacker intercepts/observes the signature bytes.

Scenario: Alice tests a dApp on testnet and signs `offer_signer_capability` granting the dApp's address signer capability (sequence_number=0). The dApp operator (or any observer of the testnet transaction) replays the identical signature bytes on mainnet where Alice's account also has sequence_number=0. The call succeeds, and the operator now holds permanent signer capability over Alice's mainnet account.

---

### Recommendation

Add `chain_id: u8` to `SignerCapabilityOfferProofChallengeV2`, mirroring the fix already applied to `RotationCapabilityOfferProofChallengeV2`:

```move
struct SignerCapabilityOfferProofChallengeV2 has copy, drop {
    chain_id: u8,          // add this field
    sequence_number: u64,
    source_address: address,
    recipient_address: address,
}
```

And populate it in `offer_signer_capability`:

```move
let proof_challenge = SignerCapabilityOfferProofChallengeV2 {
    chain_id: chain_id::get(),   // add this
    sequence_number: get_sequence_number(source_address),
    source_address,
    recipient_address,
};
```

Because this changes the signed message format, existing off-chain signers must be updated. A versioned migration (V3 struct + deprecation of V2) is the safest path.

---

### Proof of Concept

```
// Testnet (chain_id = 2), Alice's sequence_number = 0
// Alice signs:
SignerCapabilityOfferProofChallengeV2 {
    sequence_number: 0,
    source_address: 0xALICE,
    recipient_address: 0xATTACKER,
}
// BCS bytes (wrapped in SignedMessage<SignerCapabilityOfferProofChallengeV2>):
// [type_info bytes][0x00 0x00 0x00 0x00 0x00 0x00 0x00 0x00][alice_addr][attacker_addr]
// Signature: SIG_TESTNET

// Mainnet (chain_id = 1), Alice's sequence_number = 0 (same)
// Attacker submits:
account::offer_signer_capability(
    SIG_TESTNET,          // ← identical bytes, no chain_id in message
    0,                    // ED25519_SCHEME
    alice_pubkey_bytes,
    0xATTACKER
)
// → succeeds; Alice's mainnet signer_capability_offer.for = 0xATTACKER

// Attacker then calls:
account::create_authorized_signer(&attacker_signer, 0xALICE)
// → returns signer for Alice's mainnet account
// Attacker drains all APT and fungible assets from Alice's mainnet account.
```

### Citations

**File:** aptos-move/framework/aptos-framework/sources/account/account.move (L145-159)
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

    struct SignerCapabilityOfferProofChallengeV2 has copy, drop {
        sequence_number: u64,
        source_address: address,
        recipient_address: address,
    }
```

**File:** aptos-move/framework/aptos-framework/sources/account/account.move (L965-972)
```text
        // Proof that this account intends to delegate its signer capability to another account.
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

**File:** aptos-move/framework/aptos-stdlib/sources/cryptography/ed25519.move (L142-149)
```text
    public fun signature_verify_strict_t<T: drop>(signature: &Signature, public_key: &UnvalidatedPublicKey, data: T): bool {
        let encoded = SignedMessage {
            type_info: type_info::type_of<T>(),
            inner: data,
        };

        signature_verify_strict_internal(signature.bytes, public_key.bytes, bcs::to_bytes(&encoded))
    }
```

**File:** aptos-move/e2e-move-tests/src/tests/offer_signer_capability.rs (L14-22)
```rust
#[derive(Serialize, Deserialize)]
struct SignerCapabilityOfferProofChallengeV2 {
    account_address: AccountAddress,
    module_name: String,
    struct_name: String,
    sequence_number: u64,
    source_address: AccountAddress,
    recipient_address: AccountAddress,
}
```

**File:** aptos-move/e2e-move-tests/src/tests/offer_rotation_capability.rs (L16-25)
```rust
#[derive(Serialize, Deserialize)]
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
