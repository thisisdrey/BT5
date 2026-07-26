### Title
Missing `chain_id` in `MultisigAccountCreationMessage` Enables Cross-Chain Replay of Owner Signatures — (File: `aptos-move/framework/aptos-framework/sources/multisig_account.move`)

### Summary
`MultisigAccountCreationMessage` omits the `chain_id` field that its sibling struct `MultisigAccountCreationWithAuthKeyRevocationMessage` explicitly includes to prevent cross-chain replay. Owner signatures collected on testnet can be submitted in a mainnet transaction to create an unauthorized multisig account on mainnet.

### Finding Description

In `multisig_account.move`, two message structs are defined for multisig account creation:

```move
struct MultisigAccountCreationMessage has copy, drop {
    // Account address is included to prevent cross-account replay
    account_address: address,
    sequence_number: u64,
    owners: vector<address>,
    num_signatures_required: u64,
    // ← NO chain_id field
}

struct MultisigAccountCreationWithAuthKeyRevocationMessage has copy, drop {
    // Chain id is included to prevent cross-chain replay.
    chain_id: u8,   // ← explicitly present
    account_address: address,
    sequence_number: u64,
    owners: vector<address>,
    num_signatures_required: u64,
}
``` [1](#0-0) 

The developer comment on `MultisigAccountCreationWithAuthKeyRevocationMessage` explicitly states: *"Chain id is included to prevent cross-chain replay."* That comment and the field are both absent from `MultisigAccountCreationMessage`. Since owners sign the BCS-serialized form of this struct off-chain, and the on-chain verifier reconstructs and checks the same struct, a signature produced on testnet (chain_id=2) is byte-for-byte identical to what would be accepted on mainnet (chain_id=1) — the chain is never bound into the signed payload.

By contrast, the Ethereum SIWE derivable account correctly embeds `chain_id::get()` as a numeric field in its signed message:

```move
message.append(b"\nChain ID: ");
message.append(*string_utils::to_string(&chain_id::get()).bytes());
``` [2](#0-1) 

The Sui/Solana derivable accounts use `network_name()` (which maps chain_id to a human-readable string) in the signed message body, providing partial chain binding. The multisig creation message provides none. [3](#0-2) 

### Impact Explanation

An attacker who obtains valid owner signatures for a `MultisigAccountCreationMessage` on testnet can submit those same signatures in a mainnet transaction. The on-chain verifier will accept them because the signed message contains no `chain_id` field. This results in an unauthorized multisig account being created on mainnet with the same owners, address, and parameters as the testnet account — without the owners' explicit mainnet consent. If the attacker is one of the owners, they gain co-control of a mainnet multisig account that other owners did not knowingly authorize on mainnet.

### Likelihood Explanation

The attack is realistic for users who prototype multisig setups on testnet before deploying on mainnet. The replay succeeds when:
1. The sender account has the same `sequence_number` on both chains (e.g., both at 0 for a fresh account — the common case for testnet-first users).
2. The multisig account does not already exist at that address on mainnet.
3. The attacker can observe or obtain the owner signatures (e.g., by being one of the owners, or by monitoring testnet transactions).

Aptos addresses are deterministic from public keys, so the same key maps to the same address on both chains, satisfying the `account_address` field match automatically.

### Recommendation

Add `chain_id: u8` to `MultisigAccountCreationMessage` and populate it with `chain_id::get()` when constructing the message for signing, exactly as already done in `MultisigAccountCreationWithAuthKeyRevocationMessage`:

```diff
struct MultisigAccountCreationMessage has copy, drop {
+   // Chain id is included to prevent cross-chain replay.
+   chain_id: u8,
    account_address: address,
    sequence_number: u64,
    owners: vector<address>,
    num_signatures_required: u64,
}
```

### Proof of Concept

1. On testnet, initiate multisig account creation for address `A` (sequence_number=0) with owners `[Alice, Bob]` and threshold 2.
2. Alice and Bob sign the BCS-serialized `MultisigAccountCreationMessage { account_address: A, sequence_number: 0, owners: [Alice, Bob], num_signatures_required: 2 }`.
3. On mainnet, where address `A` also has sequence_number=0 and no multisig account yet, submit a transaction calling the multisig account creation entry function with the same owners, threshold, and the testnet signatures.
4. The on-chain verifier reconstructs `MultisigAccountCreationMessage { account_address: A, sequence_number: 0, owners: [Alice, Bob], num_signatures_required: 2 }` — identical BCS bytes to what Alice and Bob signed on testnet, because `chain_id` is absent from the struct.
5. Signature verification passes; the multisig account is created on mainnet without Alice's and Bob's explicit mainnet consent. [4](#0-3)

### Citations

**File:** aptos-move/framework/aptos-framework/sources/multisig_account.move (L211-235)
```text
        // Account address is included to prevent cross-account replay (when multiple accounts share the same auth key).
        account_address: address,
        // Sequence number is not needed for replay protection as the multisig account can only be created once.
        // But it's included to ensure timely execution of account creation.
        sequence_number: u64,
        // The list of owners for the multisig account.
        owners: vector<address>,
        // The number of signatures required (signature threshold).
        num_signatures_required: u64,
    }

    /// Used only for verifying multisig account creation on top of existing accounts and rotating the auth key to 0x0.
    struct MultisigAccountCreationWithAuthKeyRevocationMessage has copy, drop {
        // Chain id is included to prevent cross-chain replay.
        chain_id: u8,
        // Account address is included to prevent cross-account replay (when multiple accounts share the same auth key).
        account_address: address,
        // Sequence number is not needed for replay protection as the multisig account can only be created once.
        // But it's included to ensure timely execution of account creation.
        sequence_number: u64,
        // The list of owners for the multisig account.
        owners: vector<address>,
        // The number of signatures required (signature threshold).
        num_signatures_required: u64,
    }
```

**File:** aptos-move/framework/aptos-framework/sources/account/common_account_abstractions/ethereum_derivable_account.move (L131-132)
```text
        message.append(b"\nChain ID: ");
        message.append(*string_utils::to_string(&chain_id::get()).bytes());
```

**File:** aptos-move/framework/aptos-framework/sources/account/common_account_abstractions/common_account_abstractions_utils.move (L14-28)
```text
    public(friend) fun network_name(): vector<u8> {
        let chain_id = chain_id::get();
        if (chain_id == 1) {
            b"mainnet"
        } else if (chain_id == 2) {
            b"testnet"
        } else if (chain_id == 4) {
            b"local"
        } else {
            let network_name = &mut vector[];
            network_name.append(b"custom network: ");
            network_name.append(*string_utils::to_string(&chain_id).bytes());
            *network_name
        }
    }
```
