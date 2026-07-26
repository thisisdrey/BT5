### Title
Missing `chain_id` in `SignerCapabilityOfferProofChallengeV2` Enables Cross-Chain Replay of Signer Capability Grants — (`aptos-move/framework/aptos-framework/sources/account/account.move`)

---

### Summary

`SignerCapabilityOfferProofChallengeV2` — the signed proof-of-intent struct used by `offer_signer_capability` — omits the `chain_id` field that its sibling struct `RotationCapabilityOfferProofChallengeV2` explicitly includes for cross-chain replay prevention. A valid proof-challenge signature produced on one Aptos chain (e.g., testnet) is therefore also valid on any other Aptos chain (e.g., mainnet) where the same account address and sequence number exist, allowing a malicious contract to replay the testnet authorization on mainnet and obtain an unauthorized `SignerCapability` over the victim's account.

---

### Finding Description

The two V2 proof-challenge structs are defined side-by-side in `account.move`:

```move
/// "This V2 struct adds the `chain_id` and `source_address` to the challenge message,
///  which prevents replaying the challenge message."
struct RotationCapabilityOfferProofChallengeV2 has drop {
    chain_id: u8,          // ← present
    sequence_number: u64,
    source_address: address,
    recipient_address: address,
}

struct SignerCapabilityOfferProofChallengeV2 has copy, drop {
    // chain_id is absent ← vulnerability
    sequence_number: u64,
    source_address: address,
    recipient_address: address,
}
``` [1](#0-0) 

`offer_signer_capability` constructs the challenge without `chain_id` and verifies the caller-supplied signature against it:

```move
let proof_challenge = SignerCapabilityOfferProofChallengeV2 {
    sequence_number: get_sequence_number(source_address),
    source_address,
    recipient_address,
};
verify_signed_message(
    source_address, account_scheme, account_public_key_bytes,
    signer_capability_sig_bytes, proof_challenge);
``` [2](#0-1) 

`verify_signed_message` wraps the struct in a `SignedMessage<T>` (adding type-info for domain separation between struct types) and verifies the Ed25519/MultiEd25519 signature, but performs no chain-id check: [3](#0-2) 

Because `type_info` is identical on every Aptos chain for the same module/struct, and because `sequence_number`, `source_address`, and `recipient_address` can all be identical across chains (especially for fresh accounts at sequence number 0), a signature produced on testnet is cryptographically indistinguishable from one produced on mainnet.

The deprecated predecessor `SignerCapabilityOfferProofChallenge` had only `sequence_number` + `recipient_address`. The V2 upgrade added `source_address` but — unlike the parallel upgrade of `RotationCapabilityOfferProofChallenge` → `RotationCapabilityOfferProofChallengeV2` — omitted `chain_id`. [4](#0-3) 

The Rust e2e test for `offer_rotation_capability` explicitly includes `chain_id` in its proof struct; the analogous test for `offer_signer_capability` does not: [5](#0-4) [6](#0-5) 

---

### Impact Explanation

`SignerCapability` grants its holder the ability to call `create_signer_with_capability`, producing a fully-authorized `signer` for the victim's account. This is equivalent to owning the account's private key: the attacker can transfer all APT, fungible assets, and token objects, publish modules, rotate the authentication key, and perform any other privileged operation. The impact is **direct theft and permanent loss of all user-controlled on-chain assets**.

---

### Likelihood Explanation

The attack requires two conditions:

1. **Alice signed a `SignerCapabilityOfferProofChallengeV2` on a non-mainnet chain.** This happens whenever Alice calls `offer_signer_capability` on testnet (or any devnet/preview chain). The signature is permanently visible in the on-chain transaction.

2. **Alice interacts with a malicious contract on mainnet that calls `offer_signer_capability` with Alice's signer.** The proof-challenge mechanism exists precisely because Move allows a contract that receives `&signer` to call `offer_signer_capability` on the account owner's behalf. A malicious contract can hardcode the testnet proof-challenge signature and the recipient address, then invoke `offer_signer_capability` when Alice calls it on mainnet.

Both conditions are realistic: many users test on testnet before mainnet, and the testnet signature is public. The sequence number is 0 for any account that has not yet sent a transaction on mainnet, which is common for newly funded accounts.

---

### Recommendation

Add `chain_id` to `SignerCapabilityOfferProofChallengeV2`, matching `RotationCapabilityOfferProofChallengeV2`:

```move
struct SignerCapabilityOfferProofChallengeV2 has copy, drop {
    chain_id: u8,          // ← add this field
    sequence_number: u64,
    source_address: address,
    recipient_address: address,
}
```

Update `offer_signer_capability` to populate it:

```move
let proof_challenge = SignerCapabilityOfferProofChallengeV2 {
    chain_id: chain_id::get(),   // ← add this line
    sequence_number: get_sequence_number(source_address),
    source_address,
    recipient_address,
};
```

Because this changes the BCS-serialized layout of the struct, existing off-chain tooling that constructs the proof challenge bytes must be updated accordingly (as was done when `RotationCapabilityOfferProofChallengeV2` was introduced).

---

### Proof of Concept

**Setup**: Alice has the same Ed25519 key pair on testnet (chain_id = 2) and mainnet (chain_id = 1). Her account address and sequence number (0) are identical on both chains. Attacker controls `bob_addr`.

**Step 1 — Obtain testnet signature (public on-chain).**  
Alice calls `offer_signer_capability` on testnet to offer Bob a signer capability. The transaction's `signer_capability_sig_bytes` argument is a signature over:

```
BCS( SignedMessage {
    type_info: TypeInfo { account_address: 0x1, module_name: "account",
                          struct_name: "SignerCapabilityOfferProofChallengeV2" },
    inner: SignerCapabilityOfferProofChallengeV2 {
        sequence_number: 0,
        source_address: alice_addr,
        recipient_address: bob_addr,
    }
} )
```

No `chain_id` is included. The signature is visible in the testnet transaction.

**Step 2 — Deploy malicious contract on mainnet.**  
The attacker deploys a Move module on mainnet that hardcodes `testnet_sig_bytes`, `alice_pk_bytes`, and `bob_addr`, and exposes an entry function:

```move
public entry fun drain(alice: &signer) {
    account::offer_signer_capability(
        alice,
        TESTNET_SIG_BYTES,   // replayed from testnet
        0,                   // ED25519_SCHEME
        ALICE_PK_BYTES,
        BOB_ADDR,
    );
}
```

**Step 3 — Alice calls the malicious contract on mainnet.**  
Alice's mainnet account has sequence number 0 and the same auth key. The proof challenge constructed inside `offer_signer_capability` is byte-for-byte identical to the one Alice signed on testnet (no `chain_id` to differentiate). `verify_signed_message` succeeds.

**Step 4 — Attacker drains Alice's mainnet account.**  
Bob calls `create_authorized_signer(bob, alice_addr)` to obtain Alice's signer, then transfers all APT and fungible assets to the attacker. [7](#0-6)

### Citations

**File:** aptos-move/framework/aptos-framework/sources/account/account.move (L133-159)
```text
    /// Deprecated struct - newest version is `RotationCapabilityOfferProofChallengeV2`
    struct RotationCapabilityOfferProofChallenge has drop {
        sequence_number: u64,
        recipient_address: address,
    }

    /// Deprecated struct - newest version is `SignerCapabilityOfferProofChallengeV2`
    struct SignerCapabilityOfferProofChallenge has drop {
        sequence_number: u64,
        recipient_address: address,
    }

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

**File:** aptos-move/framework/aptos-framework/sources/account/account.move (L954-977)
```text
    public entry fun offer_signer_capability(
        account: &signer,
        signer_capability_sig_bytes: vector<u8>,
        account_scheme: u8,
        account_public_key_bytes: vector<u8>,
        recipient_address: address
    ) acquires Account {
        let source_address = signer::address_of(account);
        ensure_resource_exists(source_address);
        assert!(exists_at(recipient_address), error::not_found(EACCOUNT_DOES_NOT_EXIST));

        // Proof that this account intends to delegate its signer capability to another account.
        let proof_challenge = SignerCapabilityOfferProofChallengeV2 {
            sequence_number: get_sequence_number(source_address),
            source_address,
            recipient_address,
        };
        verify_signed_message(
            source_address, account_scheme, account_public_key_bytes, signer_capability_sig_bytes, proof_challenge);

        // Update the existing signer capability offer or put in a new signer capability offer for the recipient.
        let account_resource = &mut Account[source_address];
        account_resource.signer_capability_offer.for.swap_or_fill(recipient_address);
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
