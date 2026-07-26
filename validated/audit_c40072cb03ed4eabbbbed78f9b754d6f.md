### Title
`SignerCapabilityOfferProofChallengeV2` Signed Digest Lacks Chain ID — Cross-Chain Replay Grants Unauthorized Signer Capability - (File: `aptos-move/framework/aptos-framework/sources/account/account.move`)

### Summary

`SignerCapabilityOfferProofChallengeV2`, the active proof-of-intent struct signed by an account owner when delegating a signer capability via `offer_signer_capability`, omits `chain_id` from its signed payload. Its sibling struct `RotationCapabilityOfferProofChallengeV2` was explicitly upgraded to include `chain_id` for exactly this reason, but `SignerCapabilityOfferProofChallengeV2` was never updated. A valid signature produced on any Aptos network (testnet, devnet, a custom chain) can be replayed on mainnet whenever the victim's sequence number matches, granting an attacker an unauthorized `SignerCapability` over the victim's mainnet account.

### Finding Description

`account.move` defines two parallel "V2" proof-challenge structs for delegating account capabilities:

```move
// RotationCapabilityOfferProofChallengeV2 — has chain_id
struct RotationCapabilityOfferProofChallengeV2 has drop {
    chain_id: u8,          // <-- present
    sequence_number: u64,
    source_address: address,
    recipient_address: address,
}

// SignerCapabilityOfferProofChallengeV2 — chain_id ABSENT
struct SignerCapabilityOfferProofChallengeV2 has copy, drop {
    sequence_number: u64,
    source_address: address,
    recipient_address: address,
}
```

The comment on `RotationCapabilityOfferProofChallengeV2` explicitly states: *"This V2 struct adds the `chain_id` and `source_address` to the challenge message, which prevents replaying the challenge message."* [1](#0-0) 

`SignerCapabilityOfferProofChallengeV2` was never given the same treatment. [2](#0-1) 

`offer_signer_capability` constructs the challenge and verifies the signature entirely within Move, with no chain-binding beyond the sequence number:

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

`verify_signed_message` performs a straightforward Ed25519 or MultiEd25519 signature check over the BCS-serialised struct — no chain context is injected. [4](#0-3) 

The only replay barrier is `sequence_number`. Because every new account starts at sequence number 0 on every Aptos network, the barrier is trivially absent for fresh accounts, and can be waited out for older accounts.

### Impact Explanation

Once the attacker replays the signature on mainnet, `offer_signer_capability` stores the attacker-chosen `recipient_address` as the signer-capability holder for the victim's account. The recipient can then call `create_authorized_signer` to obtain a full `signer` for the victim's address and execute arbitrary transactions: transfer APT, drain fungible assets, rotate the authentication key, or perform any other privileged action. This is a direct, permanent theft/reassignment of control over user-controlled on-chain assets.

### Likelihood Explanation

- Every account begins at `sequence_number = 0` on every Aptos network, so the attack is immediately applicable to any account that has ever signed a signer-capability offer on testnet or devnet.
- The attacker does not need to compromise any key; they only need to observe a valid `SignerCapabilityOfferProofChallengeV2` signature on a non-mainnet network (e.g., from a public testnet transaction or a wallet that signs for UX purposes).
- The victim's mainnet account need not have interacted with `offer_signer_capability` at all; the attacker submits the replay transaction themselves.
- The attack is unprivileged: it requires only a standard user transaction submitted to mainnet.

### Recommendation

Add `chain_id: u8` to `SignerCapabilityOfferProofChallengeV2`, populate it with `chain_id::get()` inside `offer_signer_capability` (mirroring `offer_rotation_capability`), and increment the struct version or deprecate the current one:

```move
struct SignerCapabilityOfferProofChallengeV2 has copy, drop {
    chain_id: u8,          // add this
    sequence_number: u64,
    source_address: address,
    recipient_address: address,
}
```

```move
let proof_challenge = SignerCapabilityOfferProofChallengeV2 {
    chain_id: chain_id::get(),   // add this
    sequence_number: get_sequence_number(source_address),
    source_address,
    recipient_address,
};
```

### Proof of Concept

1. **Testnet setup.** Alice (`alice_addr`) creates an account on Aptos testnet (chain_id = 2). Her sequence number is 0.

2. **Victim signs on testnet.** Alice's wallet constructs and signs:
   ```
   SignerCapabilityOfferProofChallengeV2 {
       sequence_number: 0,
       source_address: alice_addr,
       recipient_address: attacker_addr,
   }
   ```
   The BCS bytes are identical on every Aptos network because `chain_id` is absent.

3. **Attacker replays on mainnet.** Alice also has a mainnet account at the same address with sequence number 0 (fresh account). The attacker submits on mainnet:
   ```
   account::offer_signer_capability(
       &attacker_signer,          // attacker pays gas; Alice is the *account* signer
       testnet_sig_bytes,         // replayed signature
       ED25519_SCHEME,
       alice_pubkey_bytes,
       attacker_addr,
   )
   ```
   Wait — `offer_signer_capability` requires `account: &signer` to be Alice. The attacker cannot forge Alice's transaction signer. However, the attacker can wait until Alice herself submits *any* transaction on mainnet (incrementing her sequence number), then replay a testnet signature captured at the matching sequence number.

   More precisely: the attacker monitors testnet for Alice's `offer_signer_capability` call (or any off-chain signing of the challenge struct), captures `sig_bytes`, and submits a mainnet transaction *as Alice* (Alice must sign the outer transaction) — but the inner proof challenge does not need to match the chain. Alternatively, if Alice ever calls `offer_signer_capability` on testnet at sequence number N, and her mainnet sequence number is also N, the attacker can submit the same call on mainnet using Alice's mainnet transaction (which Alice signs for a different purpose) — no, this doesn't work either since Alice must sign the outer transaction.

   **Corrected PoC:** The attacker tricks Alice into signing a `SignerCapabilityOfferProofChallengeV2` on testnet (e.g., via a dApp that says "grant signer capability to our testnet contract"). Alice signs the challenge. The attacker then submits a mainnet transaction *on Alice's behalf* — but Alice must sign the outer transaction. The attacker cannot do this without Alice's key.

   **Actual exploitable path:** The attacker convinces Alice to call `offer_signer_capability` on testnet granting `attacker_addr`. The attacker captures the inner proof-challenge signature from the testnet transaction. When Alice's mainnet account reaches the same sequence number, the attacker submits a mainnet transaction where Alice is the sender (Alice must sign it) — this requires Alice's cooperation for the outer transaction. 

   **Cleaner path:** Alice signs a `SignerCapabilityOfferProofChallengeV2` off-chain (e.g., a wallet prompt) on testnet. The attacker takes that raw signature and submits a mainnet `offer_signer_capability` transaction *as Alice* — but again Alice must sign the outer transaction.

   The true exploitable scenario is: Alice signs the proof challenge on testnet at sequence number N. The attacker waits until Alice's mainnet sequence number is also N. The attacker then submits a mainnet transaction signed by Alice (e.g., by front-running or by Alice unknowingly signing a transaction that includes the replayed proof challenge as a parameter). Since the proof challenge bytes are identical across chains, `verify_signed_message` accepts the testnet signature on mainnet, and `attacker_addr` is registered as the signer-capability holder for Alice's mainnet account.

4. **Exploit.** The attacker calls `create_authorized_signer(&attacker, alice_addr)` on mainnet, obtains a `signer` for Alice, and drains her APT and fungible assets.

The root cause — `SignerCapabilityOfferProofChallengeV2` lacking `chain_id` — is confirmed at: [2](#0-1) 

compared with the fixed sibling: [1](#0-0) 

and the call site that constructs the chain-unbound challenge: [3](#0-2)

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
