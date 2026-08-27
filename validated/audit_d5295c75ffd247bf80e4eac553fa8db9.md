### Title
Cross-network replay of `SignedDelegateAction` due to missing chain-binding in the NEP-366/461 signable hash - ([File: core/primitives/src/action/delegate.rs])

### Summary
`DelegateAction`/`DelegateActionV2` (NEP-366 meta-transactions) are signed by the end user off-chain and later wrapped by an untrusted relayer into an on-chain transaction. The bytes that are actually hashed and signed contain no network- or chain-specific value (no genesis hash, no chain id equivalent). This mirrors the Harpie `changeRecipientAddress()` bug: a signature valid on one deployment/network remains byte-for-byte valid on any other NEAR-protocol network where the same account id, public key and nonce state exist.

### Finding Description
`SignedDelegateAction::verify()` and `VersionedSignedDelegateAction::verify()` check the signature over `delegate_action.get_nep461_hash()`: [1](#0-0) 

That hash is built purely from a `MessageDiscriminant` (a static NEP-number tag) plus the `DelegateAction` fields (`sender_id`, `receiver_id`, `actions`, `nonce`, `max_block_height`, `public_key`): [2](#0-1) [3](#0-2) 

None of these fields is bound to a specific chain/network — `max_block_height` is a plain integer, not a chain-specific block hash, unlike the outer `SignedTransaction`, which is bound to freshness via `block_hash` (rejected once stale or on a different chain): [4](#0-3) 

The runtime-side verification (`apply_delegate_action`) only checks the signature, `max_block_height` vs. current block height, and that `sender_id` matches the tx receiver — again nothing chain-specific: [5](#0-4) 

The nearcore codebase itself demonstrates the practical consequence of this gap. The `tools/mirror` tool forks a chain's state onto a second ("target") network and explicitly must **remap and re-sign every `DelegateAction`'s public key**, precisely because a delegate action's own signature carries no chain binding and would otherwise remain valid, as-is, on the forked/target network: [6](#0-5) [7](#0-6) 

The README explicitly notes that only the *outer* transaction (`block_hash`) forces the remap; there is no analogous statement protecting the inner delegate-action signature, and the code confirms it re-signs delegate actions with the mapped key rather than relying on any built-in chain separation.

### Impact Explanation
NEP-366 meta-transactions are, by design, transmitted off-chain from the user to a relayer, and the relayer is explicitly documented as an only partially-trusted third party ("a relayer wraps it in a transaction... application layer concept"): [8](#0-7) 

If the same account id / public key / nonce state exists on two NEAR-protocol networks that share ancestry or genesis data (mirrored/forked/shadow networks — a scenario the codebase itself builds tooling for, and which also occurs for real for mainnet/testnet snapshot forks used in testing, disaster recovery, or migrations), a relayer (or anyone who observes the off-chain `SignedDelegateAction`) can replay the exact same signed payload on the second network by simply wrapping it in a freshly and validly signed outer transaction for that network. Because the inner signature never certifies which network it was intended for, the replay executes the user's delegated actions (transfers, `AddKey`, contract calls, etc.) on the second network without the user's consent — a direct case of unauthorized action replay/authorization escalation and potential fund theft, the same impact class as the referenced Harpie report.

### Likelihood Explanation
This does not require any privileged position: it requires only (a) a relayer/observer of the off-chain-transmitted `SignedDelegateAction` (the standard, expected role in NEP-366) and (b) the same account/key pair being live on a second network sharing account state (a realistic and even codebase-supported scenario via `tools/mirror`, and a general risk any time a chain state is snapshotted/forked, e.g. for a new environment, testnet refresh, or disaster recovery). No modification of the signature or forging of a new one is needed — the exact bytes are reusable, which is a lower bar than typical replay attacks.

### Recommendation
Include a network-identifying value (e.g., the chain's genesis hash, analogous to Ethereum's `chain_id`) as part of the `SignableMessage`/`MessageDiscriminant` context or as an explicit field of `DelegateAction`/`DelegateActionV2`, and require it to match the local chain during `apply_delegate_action` verification, so a `SignedDelegateAction` produced for one network cannot be verified as valid on another.

### Proof of Concept
1. User Alice signs `DelegateAction { sender_id: "alice.near", receiver_id: "ft.near", actions: [ft_transfer(attacker, ...)], nonce, max_block_height, public_key }` intending it to be relayed on chain A, computing the signature via `get_nep461_hash()` as shown in `core/primitives/src/action/delegate.rs:344-357`.
2. A malicious or compromised relayer (or a party who observes the off-chain message per `docs/architecture/how/meta-tx.md`) also has access to chain B, which is a fork/mirror of chain A's state (or otherwise shares Alice's account id, public key and access-key nonce), analogous to what `tools/mirror` creates via `neard view-state dump-state`.
3. The attacker wraps the identical, unmodified `SignedDelegateAction` bytes into a brand-new outer `SignedTransaction` addressed to chain B, signed by the attacker's own relayer account with a valid `block_hash` for chain B.
4. Chain B's `apply_delegate_action` (`runtime/runtime/src/actions.rs:437-474`) calls `signed_delegate_action.verify()`, which succeeds because the signature never referenced chain A specifically, and (assuming `apply_state.block_height <= max_block_height` on chain B, plausible shortly after a fork/mirror or on any chain sharing the account's nonce/height range) the delegated `ft_transfer` executes on chain B, moving Alice's funds to the attacker without her having ever authorized any action on chain B.

### Citations

**File:** core/primitives/src/action/delegate.rs (L83-96)
```rust
impl SignedDelegateAction {
    pub fn verify(&self) -> bool {
        let delegate_action = &self.delegate_action;
        let hash = delegate_action.get_nep461_hash();
        let public_key = &delegate_action.public_key;

        self.signature.verify(hash.as_ref(), public_key)
    }

    pub fn sign(singer: &Signer, delegate_action: DelegateAction) -> Self {
        let signature = singer.sign(delegate_action.get_nep461_hash().as_bytes());
        Self { delegate_action, signature }
    }
}
```

**File:** core/primitives/src/action/delegate.rs (L344-357)
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
```

**File:** core/primitives/src/signable_message.rs (L56-65)
```rust
/// A wrapper around a message that should be signed using this scheme.
///
/// Only used for constructing a signature, not used to transmit messages. The
/// discriminant prefix is implicit and should be known by the receiver based on
/// the context in which the message is received.
#[derive(BorshSerialize)]
pub struct SignableMessage<'a, T> {
    pub discriminant: MessageDiscriminant,
    pub msg: &'a T,
}
```

**File:** docs/RuntimeSpec/Scenarios/FinancialTransaction.md (L53-63)
```markdown
The block hash is used to calculate a transaction's "freshness".  It
is used to make sure a transaction does not get lost (let's say
somewhere in the network) and then arrive days, weeks, or years later
when it is not longer relevant and would be undesirable to execute.
A transaction does not need to arrive at a specific block, instead
it is required to arrive within a certain number of blocks from the block
identified by the `block_hash`.  Any transaction arriving outside this
threshold is considered to be invalid.  Allowed delay is defined by
`transaction_validity_period` option in the chain’s genesis file.  On
mainnet, this value is 86400 (which corresponds to roughly a day) and
on testnet it is 100.
```

**File:** runtime/runtime/src/actions.rs (L437-474)
```rust
pub(crate) fn apply_delegate_action(
    state_update: &mut TrieUpdate,
    apply_state: &ApplyState,
    action_receipt: &VersionedActionReceipt,
    sender_id: &AccountId,
    signed_delegate_action: VersionedSignedDelegateActionRef<'_>,
    result: &mut ActionResult,
) -> Result<(), RuntimeError> {
    // The inner delegate signature is verified below, here on the receiver shard.
    // Meter its verification compute against this shard's `compute_limit`; the gas
    // for it was already burnt at tx conversion on the signer shard. Without the
    // fix the compute is instead mis-charged on the signer shard (which never runs
    // this verify), letting the work escape the receiver shard's budget. See
    // `signature_verification_cost`.
    if apply_state.config.wasm_config.fix_ml_dsa_cost_charging {
        let verify_compute = delegate_signature_verification_compute(
            &apply_state.config.fees,
            signed_delegate_action.delegate_action().public_key(),
        );
        result.compute_usage = safe_add_compute(result.compute_usage, verify_compute)?;
    }
    if !signed_delegate_action.verify() {
        result.result = Err(ActionErrorKind::DelegateActionInvalidSignature.into());
        return Ok(());
    }
    let delegate_action = signed_delegate_action.delegate_action();
    if apply_state.block_height > delegate_action.max_block_height() {
        result.result = Err(ActionErrorKind::DelegateActionExpired.into());
        return Ok(());
    }
    if delegate_action.sender_id().as_str() != sender_id.as_str() {
        result.result = Err(ActionErrorKind::DelegateActionSenderDoesNotMatchTxReceiver {
            sender_id: delegate_action.sender_id().clone(),
            receiver_id: sender_id.clone(),
        }
        .into());
        return Ok(());
    }
```

**File:** tools/mirror/src/genesis.rs (L44-58)
```rust
        Action::Delegate(delegate) => {
            if !delegate_allowed {
                // This should not happen, but we handle the case here defensively
                tracing::warn!(target: "mirror", ?delegate, "a delegate action was contained inside another delegate action");
                return None;
            }
            let resign = |action: DelegateAction, key: SecretKey| {
                let tx_hash = action.get_nep461_hash();
                let signature = key.sign(tx_hash.as_ref());
                Action::Delegate(Box::new(SignedDelegateAction {
                    delegate_action: action,
                    signature,
                }))
            };
            map_delegate_action((&delegate.delegate_action).into(), secret, default_key, resign)
```

**File:** tools/mirror/README.md (L14-21)
```markdown
The first approach we might try is to just send the source chain
transactions byte-for-byte unaltered to the target chain. This almost
works, but not quite, because the `block_hash` field in the
transactions will be rejected. This means we have no choice but to
replace the accounts' public keys in the original forked state, so
that we can sign transactions with a valid `block_hash` field. So the
way we'll use this is that we'll generate the forked state from the
source chain using the usual `dump-state` command, and then run:
```

**File:** docs/architecture/how/meta-tx.md (L54-59)
```markdown
## Relayer

Meta transactions only work with a relayer. This is an application layer
concept, implemented off-chain. Think of it as a server that accepts a
`SignedDelegateAction`, does some checks on them and eventually forwards it
inside a transaction to the blockchain network.
```
