### Title
Cross-chain / cross-network replay of NEP-366 `DelegateAction` (meta-transaction) signatures - (File: `core/primitives/src/action/delegate.rs`)

### Summary
`SignedDelegateAction`/`VersionedSignedDelegateAction` in nearcore's meta-transaction implementation sign a hash that is scoped only to the sender/receiver account IDs, the actions, an access-key `nonce`, `max_block_height`, and the signer's `public_key`. Unlike a regular `SignedTransaction`, which binds itself to a specific chain via `block_hash` (a hash of a block that only exists on one chain, causing `InvalidChain`/`Expired` rejection elsewhere), a `DelegateAction`'s signable payload contains no chain/genesis-binding value at all. This is the exact bug class from the reference report: a signed authorization message omits a field that ties it to one specific chain, so it can be replayed on any other chain where the same account id, public key, and nonce state exist.

### Finding Description
`DelegateAction::get_nep461_hash` (and its V2 counterpart) hashes:
```
sender_id, receiver_id, actions, nonce, max_block_height, public_key
``` [1](#0-0) [2](#0-1) 

Verification simply checks the ed25519 signature against this hash and the embedded `public_key`: [3](#0-2) [4](#0-3) 

By contrast, an ordinary `SignedTransaction` embeds a `block_hash` field that is checked to belong to the current chain (`InvalidChain`) and to be recent (`Expired`), which is nearcore's substitute for an EVM-style `chainId` — a `SignedTransaction` from one nearcore-based chain is unusable on a different chain because the referenced block hash will not resolve there: [5](#0-4) [6](#0-5) [7](#0-6) 

The `DelegateAction`, however, has no such genesis/block-hash binding. Its only freshness/replay controls are:
- `nonce`, which is validated only against the *access key's* stored nonce on whatever chain processes it (a purely local, per-account counter, not chain-unique), [8](#0-7) 
- `max_block_height`, a plain integer upper bound compared against the local block height of whichever chain executes it, with no reference to a specific block or chain identity.

Because nearcore is widely reused as a base for other networks/testnets/private chains (and its own tooling, e.g. `tools/mirror`, exists specifically to clone/mirror one nearcore chain's state — including accounts, access keys and their nonces — into another), it is realistic for the same `(sender_id, public_key)` pair to exist with the same or lower access-key nonce on more than one independently operated nearcore-based chain (e.g., mainnet vs. a forked/cloned network, or two networks derived from a shared genesis snapshot). In that situation, a `SignedDelegateAction` produced and signed for use as a meta-transaction on chain A remains a fully valid, verifiable signed message on chain B, because nothing in the signed payload distinguishes the two chains. A relayer on chain B can wrap the exact same `Action::Delegate`/`Action::DelegateV2` payload into its own locally-valid `SignedTransaction` (whose own `block_hash`/nonce are chain B's, satisfying B's own replay checks) and have it accepted and executed, re-authorizing the sender's actions (e.g., `AddKey`, transfers, function calls) on chain B without the sender's consent for that specific chain.

### Impact Explanation
If exploited, an attacker (any relayer, since no special role is required to submit `Action::Delegate`) could replay a user's meta-transaction authorization on an alternate nearcore-based network where the user's account/keys coexist with matching nonce state, causing unauthorized execution of the delegated actions there (e.g., adding an attacker-controlled key via `AddKey`, or moving funds/making calls). This is an authorization-escalation / replay class of issue matching "double-spend/replay" and "authorization escalation across accounts" in the accepted impact list.

### Likelihood Explanation
Exploitability is conditioned on the existence of a second nearcore-based network that shares account/key/nonce state with the origin network — a scenario that is realistic given nearcore's genesis-cloning tooling (`tools/mirror`) and the common practice of forking/copying live network state for testnets, canary networks, or app-specific chains built on the nearcore codebase. It is not guaranteed on every deployment, so likelihood is chain-topology dependent rather than universal, but the root cause (missing chain-binding field in the signed message) is unconditionally present in the code.

### Recommendation
Include a chain-binding value in the NEP-461/NEP-366 signable payload for `DelegateAction`/`DelegateActionV2`, e.g. the genesis hash of the chain (or another network-unique identifier available to both the signer and the runtime) alongside `sender_id`, `receiver_id`, `nonce`, `max_block_height`, and `public_key`, and validate it during `SignedDelegateAction::verify`/`VersionedSignedDelegateAction::verify`. This mirrors the protection that regular `SignedTransaction.block_hash` already provides and closes the cross-chain replay gap for meta-transactions.

### Proof of Concept
1. Two nearcore-based chains, A and B, are initialized from the same (or overlapping) genesis/state snapshot such that account `alice.near` has the same full-access `public_key` and the same access-key `nonce` on both chains (achievable via genesis export/import or mirroring tooling such as `tools/mirror`).
2. Alice signs a `DelegateAction` (e.g., an `AddKey` action granting a new full-access key) intended only for use on chain A: `let signed = SignedDelegateAction::sign(&alice_signer, delegate_action);` as shown in [9](#0-8) .
3. A relayer wraps this identical `signed` payload into an `Action::Delegate` inside a fresh, locally-valid `SignedTransaction` for chain B (using chain B's own recent `block_hash` and the relayer's own nonce), following the same pattern used in tests: [10](#0-9) .
4. Chain B's runtime calls `SignedDelegateAction::verify()`/`VersionedSignedDelegateAction::verify()`, which only checks the ed25519 signature over the chain-agnostic NEP-461 hash and Alice's matching access-key nonce on chain B — both of which are satisfied — so the delegate action (e.g., adding the attacker's key) executes on chain B even though Alice never authorized any action there.

### Citations

**File:** core/primitives/src/action/delegate.rs (L83-90)
```rust
impl SignedDelegateAction {
    pub fn verify(&self) -> bool {
        let delegate_action = &self.delegate_action;
        let hash = delegate_action.get_nep461_hash();
        let public_key = &delegate_action.public_key;

        self.signature.verify(hash.as_ref(), public_key)
    }
```

**File:** core/primitives/src/action/delegate.rs (L92-95)
```rust
    pub fn sign(singer: &Signer, delegate_action: DelegateAction) -> Self {
        let signature = singer.sign(delegate_action.get_nep461_hash().as_bytes());
        Self { delegate_action, signature }
    }
```

**File:** core/primitives/src/action/delegate.rs (L176-185)
```rust
    /// Delegate action hash used for NEP-461 signature scheme which tags
    /// different messages before hashing
    ///
    /// For more details, see: [NEP-461](https://github.com/near/NEPs/pull/461)
    pub fn get_nep461_hash(&self) -> CryptoHash {
        let signable = SignableMessage::new(&self, SignableMessageType::DelegateActionV2);
        let bytes = borsh::to_vec(&signable).expect("failed to serialize");
        hash(&bytes)
    }
}
```

**File:** core/primitives/src/action/delegate.rs (L210-214)
```rust
impl VersionedSignedDelegateAction {
    pub fn verify(&self) -> bool {
        let hash = self.delegate_action.get_nep461_hash();
        self.signature.verify(hash.as_ref(), self.delegate_action.public_key())
    }
```

**File:** core/primitives/src/action/delegate.rs (L344-358)
```rust
impl DelegateAction {
    pub fn get_actions(&self) -> Vec<Action> {
        self.actions.iter().map(|a| a.clone().into()).collect()
    }

    /// Delegate action hash used for NEP-461 signature scheme which tags
    /// different messages before hashing
    ///
    /// For more details, see: [NEP-461](https://github.com/near/NEPs/pull/461)
    pub fn get_nep461_hash(&self) -> CryptoHash {
        let signable = SignableMessage::new(&self, SignableMessageType::DelegateAction);
        let bytes = borsh::to_vec(&signable).expect("Failed to deserialize");
        hash(&bytes)
    }
}
```

**File:** core/primitives/src/transaction.rs (L118-137)
```rust
#[derive(BorshSerialize, BorshDeserialize, PartialEq, Eq, Debug, Clone, ProtocolSchema)]
pub struct TransactionV1 {
    /// An account on which behalf transaction is signed
    pub signer_id: AccountId,
    /// A public key of the access key which was used to sign an account.
    /// Access key holds permissions for calling certain kinds of actions.
    pub public_key: PublicKey,
    /// Nonce is used to determine order of transaction in the pool.
    /// It increments for a combination of `signer_id` and `public_key`,
    /// and for gas key it also includes a `nonce_index`.
    pub nonce: TransactionNonce,
    /// Receiver account for this transaction
    pub receiver_id: AccountId,
    /// The hash of the block in the blockchain on top of which the given transaction is valid
    pub block_hash: CryptoHash,
    /// A list of actions to be applied
    pub actions: Vec<Action>,
    /// Controls nonce validation mode (monotonic or strict sequential).
    pub nonce_mode: NonceMode,
}
```

**File:** chain/jsonrpc/jsonrpc-tests/tests/rpc_transactions.rs (L155-174)
```rust
/// Test sending transaction based on a different fork should be rejected
#[tokio::test]
async fn test_replay_protection() {
    let setup = create_test_setup_with_node_type(NodeType::Validator);
    let client = new_client(&setup.server_addr);

    let signer = InMemorySigner::test_signer(&"test1".parse().unwrap());
    let tx = SignedTransaction::send_money(
        1,
        "test1".parse().unwrap(),
        "test2".parse().unwrap(),
        &signer,
        Balance::from_yoctonear(100),
        hash(&[1]),
    );
    let bytes = borsh::to_vec(&tx).unwrap();
    if let Ok(_) = client.broadcast_tx_commit(to_base64(&bytes)).await {
        panic!("transaction should not succeed");
    }
}
```

**File:** core/primitives/src/errors.rs (L263-266)
```rust
    /// Transaction parent block hash doesn't belong to the current chain
    InvalidChain = 10,
    /// Transaction has expired
    Expired = 11,
```

**File:** runtime/runtime/src/verifier.rs (L210-237)
```rust
/// Verify that the transaction nonce is valid.
fn verify_nonce(
    tx_nonce: Nonce,
    current_nonce: Nonce,
    block_height: Option<BlockHeight>,
    nonce_mode: NonceMode,
) -> Result<(), InvalidTxError> {
    match nonce_mode {
        NonceMode::Monotonic => {
            if tx_nonce <= current_nonce {
                return Err(InvalidTxError::InvalidNonce { tx_nonce, ak_nonce: current_nonce });
            }
        }
        NonceMode::Strict => {
            if !current_nonce.checked_add(1).is_some_and(|expected| tx_nonce == expected) {
                return Err(InvalidTxError::InvalidNonce { tx_nonce, ak_nonce: current_nonce });
            }
        }
    }
    if let Some(height) = block_height {
        let upper_bound = height
            .saturating_mul(near_primitives::account::AccessKey::ACCESS_KEY_NONCE_RANGE_MULTIPLIER);
        if tx_nonce >= upper_bound {
            return Err(InvalidTxError::NonceTooLarge { tx_nonce, upper_bound });
        }
    }
    Ok(())
}
```

**File:** integration-tests/src/env/test_env.rs (L788-798)
```rust
        let signature = inner_signer.sign(delegate_action.get_nep461_hash().as_bytes());
        let signed_delegate_action = SignedDelegateAction { delegate_action, signature };
        SignedTransaction::from_actions(
            relayer_nonce,
            relayer,
            sender,
            &relayer_signer,
            vec![Action::Delegate(Box::new(signed_delegate_action))],
            tip.last_block_hash,
        )
    }
```
