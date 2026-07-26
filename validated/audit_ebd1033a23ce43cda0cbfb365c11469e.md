### Title
Missing `chain_id` in `SignerCapabilityOfferProofChallengeV2` Enables Cross-Chain Replay of Signer Capability Grants — (`File: aptos-move/framework/aptos-framework/sources/account/account.move`)

---

### Summary

`SignerCapabilityOfferProofChallengeV2` omits `chain_id` from its signed fields, while the structurally parallel `RotationCapabilityOfferProofChallengeV2` explicitly includes it. A valid `offer_signer_capability` signature produced on testnet (or any non-mainnet chain) can be replayed verbatim on mainnet whenever the victim's account has the same sequence number on both chains. Successful replay grants the attacker an authorized signer for the victim's mainnet account, enabling arbitrary asset transfers.

---

### Finding Description

The two V2 proof-challenge structs in `account.move` were introduced together to fix the original V1 structs that lacked both `chain_id` and `source_address`. The rotation-capability variant was fixed completely:

```move
// line 147-153 — comment explicitly names chain_id as the replay-prevention field
struct RotationCapabilityOfferProofChallengeV2 has drop {
    chain_id: u8,          // ← present
    sequence_number: u64,
    source_address: address,
    recipient_address: address,
}
```

The signer-capability variant added `source_address` but silently omitted `chain_id`:

```move
// line 155-159
struct SignerCapabilityOfferProofChallengeV2 has copy, drop {
    sequence_number: u64,
    source_address: address,   // ← present
    recipient_address: address,
    // chain_id MISSING
}
```

`offer_signer_capability` constructs and verifies this challenge without any chain binding:

```move
// line 966-972
let proof_challenge = SignerCapabilityOfferProofChallengeV2 {
    sequence_number: get_sequence_number(source_address),
    source_address,
    recipient_address,
};
verify_signed_message(
    source_address, account_scheme, account_public_key_bytes,
    signer_capability_sig_bytes, proof_challenge);
```

`verify_signed_message` uses `ed25519::signature_verify_strict_t`, which signs over `type_tag(SignerCapabilityOfferProofChallengeV2) || bcs(sequence_number, source_address, recipient_address)`. Because `chain_id` is absent, the BCS-serialized challenge is identical across all Aptos chains for any account that shares the same address, key pair, and sequence number.

Once the offer is accepted on-chain, `create_authorized_signer` unconditionally mints a signer for the offerer's address:

```move
// line 1029-1039
public fun create_authorized_signer(account: &signer, offerer_address: address): signer acquires Account {
    ...
    assert!(account_resource.signer_capability_offer.for.contains(&addr), ...);
    create_signer(offerer_address)   // full signer, no further checks
}
```

---

### Impact Explanation

An attacker who holds a valid `offer_signer_capability` signature from any non-mainnet chain can submit it on mainnet and receive an unrestricted signer for the victim's account. With that signer the attacker can transfer APT, fungible assets, token objects, or any other resource owned by the victim — constituting direct, unauthorized theft of user-controlled on-chain assets.

---

### Likelihood Explanation

The preconditions are realistic:

1. **Same key pair across chains** — the overwhelming majority of Aptos users derive a single key pair and use it on both testnet and mainnet.
2. **Matching sequence number** — for a fresh mainnet account (sequence number 0) the attacker only needs to find any testnet `offer_signer_capability` transaction signed at sequence number 0 by the same key. This is trivially satisfied the first time a user ever calls `offer_signer_capability` on testnet.
3. **Signature availability** — all submitted transactions are public on-chain; the attacker simply monitors testnet for `offer_signer_capability` calls and replays them on mainnet.

---

### Recommendation

Add `chain_id: chain_id::get()` to `SignerCapabilityOfferProofChallengeV2`, mirroring the fix already applied to `RotationCapabilityOfferProofChallengeV2`:

```move
struct SignerCapabilityOfferProofChallengeV2 has copy, drop {
    chain_id: u8,           // add this field
    sequence_number: u64,
    source_address: address,
    recipient_address: address,
}
```

And update `offer_signer_capability` to populate it:

```move
let proof_challenge = SignerCapabilityOfferProofChallengeV2 {
    chain_id: chain_id::get(),   // add this line
    sequence_number: get_sequence_number(source_address),
    source_address,
    recipient_address,
};
```

Because the struct type tag is part of the signed message, this is a breaking change for any off-chain signer that constructs the challenge bytes manually; those signers must be updated to include `chain_id` as the first BCS field.

---

### Proof of Concept

**Setup**: Alice uses the same Ed25519 key pair on testnet (chain ID 2) and mainnet (chain ID 1). Both accounts start at sequence number 0.

**Step 1 — Testnet**: Alice legitimately calls `offer_signer_capability` on testnet, granting Bob signer capability. The signed challenge bytes are:

```
type_tag("0x1::account::SignerCapabilityOfferProofChallengeV2")
|| bcs(0u64)                  // sequence_number = 0
|| bcs(alice_addr)            // source_address
|| bcs(bob_addr)              // recipient_address
```

**Step 2 — Mainnet replay**: Bob (or any observer) submits an identical `offer_signer_capability` transaction on mainnet with the same `signer_capability_sig_bytes`, `account_public_key_bytes`, and `recipient_address`. The challenge constructed on mainnet is byte-for-byte identical (Alice's mainnet account is also at sequence number 0, same addresses, same struct type tag, no chain ID to differ). `verify_signed_message` passes.

**Step 3 — Asset theft**: Bob calls `create_authorized_signer(&bob, alice_addr)` on mainnet, receiving a full signer for Alice's account. Bob then calls `aptos_account::transfer(alice_signer, bob_addr, alice_balance)`, draining Alice's mainnet APT.

---

**Relevant code locations:** [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3)

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

**File:** aptos-move/framework/aptos-framework/sources/account/account.move (L960-977)
```text
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
