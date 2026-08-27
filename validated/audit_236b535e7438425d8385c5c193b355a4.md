## Title
Meta-transaction (`DelegateAction` / NEP-366) signatures lack any chain/genesis-binding value, enabling cross-network replay of signed delegate actions - (`File: core/primitives/src/action/delegate.rs`, `core/primitives/src/signable_message.rs`)

### Summary
NEAR's meta-transaction scheme (NEP-366/NEP-611) signs a `DelegateAction`/`DelegateActionV2` using `get_nep461_hash()`, which hashes only `sender_id`, `receiver_id`, `actions`, `nonce`, `max_block_height`, `public_key`, plus a NEP-number/on-chain discriminant tag. Unlike a regular `SignedTransaction`, which binds to a concrete, chain-specific `block_hash`, the delegate-action payload binds only to a plain `max_block_height` integer (`BlockHeight`, i.e. a `u64`) with no genesis hash, chain id, or any other value unique to a particular network instance.

### Finding Description
`SignedDelegateAction::verify()`/`VersionedSignedDelegateAction::verify()` check the signature against `delegate_action.get_nep461_hash()`: [1](#0-0) [2](#0-1) 

The hash is computed over a `SignableMessage` composed of a `MessageDiscriminant` (which only encodes a NEP number and on-chain/off-chain flag) and the `DelegateAction`/`DelegateActionV2` struct itself: [3](#0-2) [4](#0-3) 

The `DelegateAction` struct's only "freshness"/scope field is `max_block_height: BlockHeight` (a bare integer), not an actual block hash tied to a specific chain history: [5](#0-4) 

This is different from the ordinary transaction flow, which is anchored to a concrete recent `block_hash` from the chain (giving both expiry and implicit chain-identity binding, since a block hash cannot be reproduced on a different chain). By contrast, a `DelegateAction`'s `max_block_height` is just a height number that can independently exist with the same value on any other NEAR-protocol-compatible network (a fork, a cloned/forked state used for a canary or shadow deployment, a testnet resurrected from a mainnet state snapshot, etc.), as long as sender/receiver/nonce/public key state matches.

At execution time, `apply_delegate_action()` only checks the signature, `max_block_height` against local `apply_state.block_height`, sender/receiver match, and the access-key nonce — none of which are chain-specific: [6](#0-5) 

### Impact Explanation
If two networks share the same account/access-key/nonce state at a given point (e.g., a contentious hard fork, or an operator spinning up a shadow/canary chain from a mainnet state dump for testing, which nearcore explicitly supports via state snapshots), a `DelegateAction` signed by a user for use on one network remains fully valid and replayable on the other network as long as `max_block_height` has not yet been exceeded there. Because delegate actions can carry arbitrary inner actions (transfers, function calls, key additions), a relayer or attacker could resubmit a captured `SignedDelegateAction` on the sibling network to execute unauthorized actions/transfers on the user's account on that network — a cross-chain replay resulting in double execution / unauthorized action execution, analogous to the missing `chainId` in the EIP-712 domain separator in the referenced report.

### Likelihood Explanation
This requires two networks to actually diverge from a common state (fork or state-cloned network) while both still recognize the same signed delegate payload as valid — this is a real, supported operational scenario in NEAR tooling (state snapshots/forknet-style testing, protocol forks), but it does not occur under ordinary single-chain operation. Likelihood is therefore contingent on fork/clone events rather than being exploitable purely by an ordinary client on a single live network.

### Recommendation
Include a chain-specific value (e.g., the genesis hash / chain id, similar to `EIP712`'s `chainId` in the domain separator) in the `DelegateAction`/`DelegateActionV2` payload that is hashed via `get_nep461_hash()`, so that signatures produced for one network cannot be replayed on another network that shares account/nonce state (fork, clone, or canary chain).

### Proof of Concept
1. Network A (mainnet) and Network B (a forked/cloned network sharing the same genesis state, account IDs, access keys, and nonces) both run compatible nearcore protocol versions.
2. Alice signs a `DelegateAction` (e.g., transferring tokens or adding a key) with `max_block_height` set well into the future, intended only for Network A.
3. A relayer or attacker submits the same `SignedDelegateAction` bytes on Network B.
4. `VersionedSignedDelegateActionRef::verify()` succeeds because the hash never encoded which network it was intended for — [7](#0-6)  — and `apply_delegate_action` proceeds to execute the inner actions on Network B as well, as long as `max_block_height` and the access-key nonce still validate there — [8](#0-7) .

### Citations

**File:** core/primitives/src/action/delegate.rs (L46-64)
```rust
pub struct DelegateAction {
    /// Signer of the delegated actions
    pub sender_id: AccountId,
    /// Receiver of the delegated actions.
    pub receiver_id: AccountId,
    /// List of actions to be executed.
    ///
    /// With the meta transactions MVP defined in NEP-366, nested
    /// DelegateActions are not allowed. A separate type is used to enforce it.
    pub actions: Vec<NonDelegateAction>,
    /// Nonce to ensure that the same delegate action is not sent twice by a
    /// relayer and should match for given account's `public_key`.
    /// After this action is processed it will increment.
    pub nonce: Nonce,
    /// The maximal height of the block in the blockchain below which the given DelegateAction is valid.
    pub max_block_height: BlockHeight,
    /// Public key used to sign this delegated action.
    pub public_key: PublicKey,
}
```

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

**File:** core/primitives/src/signable_message.rs (L61-65)
```rust
#[derive(BorshSerialize)]
pub struct SignableMessage<'a, T> {
    pub discriminant: MessageDiscriminant,
    pub msg: &'a T,
}
```

**File:** core/primitives/src/signable_message.rs (L97-108)
```rust
impl<'a, T: BorshSerialize> SignableMessage<'a, T> {
    pub fn new(msg: &'a T, ty: SignableMessageType) -> Self {
        let discriminant = ty.into();
        Self { discriminant, msg }
    }

    pub fn sign(&self, signer: &Signer) -> Signature {
        let bytes = borsh::to_vec(&self).expect("Failed to deserialize");
        let hash = hash(&bytes);
        signer.sign(hash.as_bytes())
    }
}
```

**File:** runtime/runtime/src/actions.rs (L437-476)
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

    validate_delegate_action_key(state_update, apply_state, delegate_action, result)?;
```
