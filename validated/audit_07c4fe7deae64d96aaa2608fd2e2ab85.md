### Title
`DelegateAction`/`DelegateActionV2` (NEP-366/NEP-461 meta-transactions) sign no chain-binding data, enabling replay of meta-transactions across a hard fork - (File: `core/primitives/src/action/delegate.rs`, `core/primitives/src/signable_message.rs`)

### Summary
The NEP-366 meta-transaction scheme signs a `DelegateAction`/`DelegateActionV2` using `SignableMessage`, which only binds `discriminant || sender_id || receiver_id || actions || nonce || max_block_height || public_key`. Unlike ordinary `SignedTransaction`s, which are bound to a specific recent `block_hash` at signing time, a `SignedDelegateAction`/`VersionedSignedDelegateAction` contains no `block_hash`, genesis hash, or any other network/chain identifier. If nearcore undergoes a contentious hard fork that produces two live chains sharing identical account/access-key state at the fork point, a previously signed delegate action remains independently valid and executable on both resulting chains.

### Finding Description
`DelegateAction::get_nep461_hash` and `VersionedDelegateActionPayload::get_nep461_hash` build the signed payload via `SignableMessage::new(&self, SignableMessageType::DelegateAction/-V2)`, which is just `discriminant || borsh(msg)`: [1](#0-0) [2](#0-1) [3](#0-2) 

The `DelegateAction` struct itself only carries `sender_id`, `receiver_id`, `actions`, `nonce`, `max_block_height`, and `public_key` — no `block_hash` or chain/network identifier: [4](#0-3) 

On execution, `apply_delegate_action` (`runtime/runtime/src/actions.rs`) only verifies: the signature over that same chain-agnostic payload, that `apply_state.block_height <= max_block_height`, that `sender_id` matches the receiver of the outer transaction, and that the access-key/gas-key nonce is monotonically increasing and below `block_height * ACCESS_KEY_NONCE_RANGE_MULTIPLIER`: [5](#0-4) [6](#0-5) 

None of these checks reference a block hash, genesis hash, or any value unique to one branch of a fork. In contrast, ordinary `SignedTransaction`s are explicitly bound to a `block_hash` chosen close to submission time and validated against `transaction_validity_period`, giving them at least a short-lived, chain-specific anchor (`docs/architecture/how/tx_routing.md`): [7](#0-6) 

Because a hard fork by definition produces two chains that share identical state (including all account balances and access-key nonces) up to the fork block, any `SignedDelegateAction`/`VersionedSignedDelegateAction` that is valid pre-fork (nonce not yet consumed, `max_block_height` not yet reached) remains independently valid and submittable — by the original relayer, or by any third party who intercepted it off-chain, since nothing prevents a non-owner from re-submitting a previously observed meta transaction — on **both** post-fork chains. Since `max_block_height` is chosen by the signer and can be set arbitrarily far in the future, and delegate-action nonces do not need to correspond to a fresh transaction's `block_hash` window, this exposure window can be long-lived, unlike ordinary transactions whose `transaction_validity_period` limits replay to a short window even absent a chain-id field.

### Impact Explanation
This is a direct analog of the reported bug class ("signature replay attacks in the case of a hard fork" via a domain separator lacking chain binding). In nearcore, exploiting it allows the same delegate action (e.g., a token transfer, `AddKey`, or any other wrapped action) to be executed once on each of the two forked chains, effectively double-spending/duplicating the effect of a single user authorization across chains — an authorization/replay violation directly enabled by the missing chain-binding field in the signed meta-transaction payload.

### Likelihood Explanation
Exploitability is conditioned on an actual nearcore hard fork producing two live, independently operating chains from a shared state snapshot — an infrequent, high-impact event but a realistic one for any blockchain protocol (contentious upgrades, chain splits). Once such an event occurs, exploitation requires no special privilege: any relayer or observer holding a previously valid, unexpired `SignedDelegateAction` can replay it on the sibling chain.

### Recommendation
Include a chain/network-binding value in the NEP-366/NEP-461 signable payload — e.g., add the current `block_hash` (analogous to `SignedTransaction`) or a persistent genesis/chain identifier — to `DelegateAction`/`DelegateActionV2`, and validate it in `apply_delegate_action` against the receiving chain's actual chain identity. This ensures a delegate action signed for one chain cannot be replayed on a forked sibling chain.

### Proof of Concept
1. Alice signs `DelegateAction { sender_id: alice, receiver_id: ft_contract, actions: [ft_transfer(bob, 100)], nonce: N, max_block_height: H (far future), public_key: alice_key }` and hands it to relayer R, per the existing `SignedDelegateAction::sign` flow: [8](#0-7) 
2. Before the delegate action executes (or after it executes but before nonce N is consumed on both branches, depending on timing), the network undergoes a hard fork at block height h < H, producing chain A and chain B with identical account/access-key state.
3. Relayer R (or anyone who observed the `SignedDelegateAction` off-chain, e.g. via a public relayer API) wraps it in an outer transaction and submits it on chain A: `apply_delegate_action` passes signature check, nonce check (`current_nonce < N`), and `max_block_height` check, since none of these differ between A and B: [5](#0-4) 
4. The same `SignedDelegateAction` bytes are submitted unmodified on chain B; all checks pass identically because no field ties the signature to a specific chain, and the 100 tokens are transferred to Bob a second time on the sibling chain.

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

**File:** core/primitives/src/action/delegate.rs (L83-95)
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
```

**File:** core/primitives/src/action/delegate.rs (L176-184)
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
```

**File:** core/primitives/src/action/delegate.rs (L349-357)
```rust
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

**File:** core/primitives/src/signable_message.rs (L61-65)
```rust
#[derive(BorshSerialize)]
pub struct SignableMessage<'a, T> {
    pub discriminant: MessageDiscriminant,
    pub msg: &'a T,
}
```

**File:** runtime/runtime/src/actions.rs (L458-474)
```rust
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

**File:** runtime/runtime/src/actions.rs (L632-650)
```rust
    if delegate_nonce.nonce() <= current_nonce {
        result.result = Err(ActionErrorKind::DelegateActionInvalidNonce {
            delegate_nonce: delegate_nonce.nonce(),
            ak_nonce: current_nonce,
        }
        .into());
        return Ok(());
    }

    let upper_bound = apply_state.block_height
        * near_primitives::account::AccessKey::ACCESS_KEY_NONCE_RANGE_MULTIPLIER;
    if delegate_nonce.nonce() >= upper_bound {
        result.result = Err(ActionErrorKind::DelegateActionNonceTooLarge {
            delegate_nonce: delegate_nonce.nonce(),
            upper_bound,
        }
        .into());
        return Ok(());
    }
```

**File:** docs/architecture/how/tx_routing.md (L31-34)
```markdown
**Fun fact:** the `Transaction` object also contains some fields to prevent
attacks: like `nonce` to prevent replay attack, and `block_hash` to limit the
validity of the transaction (it must be added within
`transaction_validity_period` (defined in genesis) blocks of `block_hash`).
```
