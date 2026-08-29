### Title
Access-key nonce regression via DeleteKey+AddKey on the same public key enables replay of an already-executed transaction - ([File: runtime/runtime/src/access_keys.rs])

### Finding Description
`add_regular_key` unconditionally reseeds `access_key.nonce` to `initial_nonce_value(block_height) = (block_height-1)*ACCESS_KEY_NONCE_RANGE_MULTIPLIER` whenever an `AddKey` action is processed, with no check of whether this public key was previously in use with a higher nonce. [1](#0-0) 

`action_delete_key`/`delete_regular_key` fully removes the `AccessKey` trie entry, so all memory of the key's prior nonce is discarded on deletion. [2](#0-1) 

`action_add_key` only rejects an `AddKey` if the key is *currently present* (`AddKeyAlreadyExists`); since `DeleteKey` and `AddKey` for the same public key can be two actions of the same transaction/receipt applied against the same mutable `TrieUpdate`, by the time `AddKey` runs the key has already been removed and re-adding the identical public key succeeds. [3](#0-2) 

Nonce validation (`verify_nonce`) only requires `tx_nonce > current_nonce` (Monotonic mode) plus an upper bound of `block_height * ACCESS_KEY_NONCE_RANGE_MULTIPLIER` computed from the height at the time of the *current* validation — not the height at which the original nonce was assigned. [4](#0-3) 

Exploit flow:
1. Attacker controls an account with one `FullAccess` key `pk`, and picks nonce `X = (H-1)*M + 1` where `H` is the block height they expect their transactions to land in (`M = ACCESS_KEY_NONCE_RANGE_MULTIPLIER = 1_000_000`).
2. tx1 = `SendMoney` signed with nonce `X`, executes at height `H`; the access key's nonce becomes `X` via `AccessKeyUpdate::Regular{nonce: tx_nonce,..}`. [5](#0-4) 
3. tx2 = `[DeleteKey(pk), AddKey(pk, FullAccess)]` signed with nonce `X+1`, executed at height `H` (or any height `H2` with `(H2-1)*M <= X`). `DeleteKey` removes the key from the shared `TrieUpdate`; `AddKey` then succeeds (key no longer present) and resets `access_key.nonce = (H2-1)*M`, which is `<= X`.
4. Attacker resubmits tx1's exact signed bytes at a later block height `H3 >> H`. `verify_nonce` now compares `X > (H2-1)*M` (true by construction) and the upper bound check uses `H3*M >> X` (trivially satisfied), so the identical transaction is accepted and applied a second time, re-executing the `SendMoney` action.

This directly contradicts the documented intended invariant in `docs/DataStructures/AccessKey.md`: "If the new access key reuses the same public key, the nonce of the new access key should be equal to the nonce of the old access key. It's required to avoid replaying old transactions again." — the current code in `add_regular_key` does not implement this rule. [6](#0-5) 

The nearcore codebase itself already documents awareness of nonce loss on key deletion (tracked as issue #6724), in a related (but distinct) scenario — deleting the signer's own last key and retrying the identical transaction: [7](#0-6) 

None of the existing checks (signature — same keypair/pk, so still valid; access-key permission — recreated identically as `FullAccess`; nonce upper bound — computed from the *resubmission* block height, not the original) stop the replay.

### Impact Explanation
This breaks the "no transaction or receipt executes twice" determinism/replay-protection invariant: the identical signed transaction (same tx hash) is accepted and applied by the runtime twice, in two different blocks. Value conservation for the signer/receiver pair is not literally broken (funds move from the same signer to the same receiver twice, both legitimately debited/credited on-chain), but the ability for an attacker to force an already-included transaction hash to execute again violates the core replay-protection guarantee the nonce scheme (and specifically `ACCESS_KEY_NONCE_RANGE_MULTIPLIER`, introduced for near/nearcore#3779) is designed to provide. This falls in the double-spend/replay bounty category: any integration that treats a specific transaction hash as a one-time, idempotent event (e.g., exchange/bridge deposit crediting logic keyed by tx hash) is fooled into observing that "one-time" event twice, and any account is free to unilaterally invalidate consensus's guarantee that a given signed message authorizes state change exactly once.

### Likelihood Explanation
Fully attacker-controlled and unprivileged: it requires only a funded account with a single `FullAccess` key, one transfer transaction, and one follow-up `DeleteKey`+`AddKey` transaction using the same key, both of which an ordinary account holder can submit via public RPC. The only timing requirement is choosing a nonce `X` close to `(H-1)*M` for an expected inclusion height `H`, which is easy since clients query the current chain height before submitting and blocks are produced roughly once per second; if the transaction lands a block or two later than expected, the attacker can simply resubmit with an adjusted nonce. This is fully repeatable per key-recreation cycle.

### Recommendation
When adding a regular (or gas) key whose public key matches a key deleted earlier in the account's history, preserve/carry-forward the maximum nonce ever observed for that `(account_id, public_key)` pair instead of unconditionally reseeding to `initial_nonce_value(block_height)`. This matches the documented intended behavior in `docs/DataStructures/AccessKey.md`. Concretely, either (a) do not fully erase the previous nonce on `DeleteKey` (retain a tombstone entry recording the last nonce, consulted by `add_regular_key`/`add_gas_key` on re-creation with the same public key and take `max(old_nonce, initial_nonce_value(block_height))`), or (b) disallow recreating a key with the exact same public key that was ever used before within the transaction-validity window.

### Proof of Concept
Runtime integration/test-loop test:
1. Create account `alice` with one `FullAccess` key `pk` (via `InMemorySigner`).
2. Query/construct `apply_state` for target height `H`; sign `tx1 = SendMoney(alice -> bob, amount)` with nonce `X = (H-1)*AccessKey::ACCESS_KEY_NONCE_RANGE_MULTIPLIER + 1`.
3. Apply `tx1` at height `H`; assert success and that `bob`'s balance increased by `amount`; assert stored `AccessKey.nonce == X`.
4. Sign `tx2 = [DeleteKey(pk), AddKey(pk, AccessKey::full_access())]` with nonce `X+1`; apply at height `H` (or `H2` with `(H2-1)*M <= X`); assert success; read back `AccessKey` for `pk` and assert `nonce == (H2-1)*M <= X`.
5. At a later height `H3` (e.g. `H+10`), resubmit `tx1`'s exact `SignedTransaction` bytes; call `validate_verify_and_charge_transaction`/`Runtime::apply` and assert it is accepted (`Ok`), not rejected with `InvalidNonce`.
6. Assert `bob`'s balance has increased by `amount` a second time (total `2*amount` delta from step 3), proving the same signed transaction executed twice.

### Citations

**File:** runtime/runtime/src/access_keys.rs (L136-147)
```rust
fn delete_regular_key(
    fee_config: &RuntimeFeesConfig,
    state_update: &mut TrieUpdate,
    account: &mut Account,
    account_id: &AccountId,
    public_key: &PublicKey,
    access_key: &AccessKey,
) {
    let storage_usage = access_key_storage_usage(fee_config, public_key, access_key);
    remove_access_key(state_update, account_id.clone(), public_key.clone());
    account.set_storage_usage(account.storage_usage().saturating_sub(storage_usage));
}
```

**File:** runtime/runtime/src/access_keys.rs (L149-192)
```rust
pub(crate) fn action_add_key(
    apply_state: &ApplyState,
    state_update: &mut TrieUpdate,
    account: &mut Account,
    result: &mut ActionResult,
    account_id: &AccountId,
    add_key: &AddKeyAction,
) -> Result<(), StorageError> {
    if get_access_key(state_update, account_id, &add_key.public_key)?.is_some() {
        result.result = Err(ActionErrorKind::AddKeyAlreadyExists {
            account_id: account_id.to_owned(),
            public_key: add_key.public_key.clone().into(),
        }
        .into());
        return Ok(());
    }

    let fee_config = &apply_state.config.fees;

    if let Some(gas_key_info) = add_key.access_key.gas_key_info() {
        add_gas_key(
            fee_config,
            state_update,
            account,
            account_id,
            &add_key.public_key,
            &add_key.access_key,
            gas_key_info,
            apply_state.block_height,
        )?;
    } else {
        add_regular_key(
            fee_config,
            state_update,
            account,
            account_id,
            &add_key.public_key,
            &add_key.access_key,
            apply_state.block_height,
        )?;
    }

    Ok(())
}
```

**File:** runtime/runtime/src/access_keys.rs (L230-241)
```rust
fn add_regular_key(
    fee_config: &RuntimeFeesConfig,
    state_update: &mut TrieUpdate,
    account: &mut Account,
    account_id: &AccountId,
    public_key: &PublicKey,
    access_key: &AccessKey,
    block_height: BlockHeight,
) -> Result<(), StorageError> {
    let mut access_key = access_key.clone();
    access_key.nonce = initial_nonce_value(block_height);
    set_access_key(state_update, account_id.clone(), public_key.clone(), &access_key);
```

**File:** runtime/runtime/src/verifier.rs (L211-236)
```rust
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
```

**File:** runtime/runtime/src/lib.rs (L303-328)
```rust
/// Describes how to update the access key after a verified transaction.
#[derive(Debug, Clone, PartialEq)]
pub enum AccessKeyUpdate {
    /// Regular tx: set access_key.nonce, update allowance if specified.
    Regular { nonce: Nonce, new_allowance: Option<Balance> },
    /// Gas key tx: set gas_key_info.balance and persist external nonce.
    GasKey { new_balance: Balance, nonce_index: NonceIndex, nonce: Nonce },
}

impl VerificationResult {
    /// Apply the state changes described by this result to the given account and access key.
    pub fn apply(&self, account: &mut Account, access_key: &mut AccessKey) {
        account.set_amount(self.new_account_amount);
        match &self.access_key_update {
            AccessKeyUpdate::Regular { nonce, new_allowance } => {
                access_key.nonce = *nonce;
                if let Some(a) = new_allowance {
                    access_key.permission.function_call_permission_mut().unwrap().allowance =
                        Some(*a);
                }
            }
            AccessKeyUpdate::GasKey { new_balance, .. } => {
                access_key.gas_key_info_mut().unwrap().balance = *new_balance;
            }
        }
    }
```

**File:** docs/DataStructures/AccessKey.md (L6-16)
```markdown
```rust
pub struct AccessKey {
    /// The nonce for this access key.
    /// NOTE: In some cases the access key needs to be recreated. If the new access key reuses the
    /// same public key, the nonce of the new access key should be equal to the nonce of the old
    /// access key. It's required to avoid replaying old transactions again.
    pub nonce: Nonce,
    /// Defines permissions for this access key.
    pub permission: AccessKeyPermission,
}
```
```

**File:** integration-tests/src/tests/standard_cases/mod.rs (L1169-1173)
```rust
        Err(err) => {
            // TODO(#6724): This is a wrong error, the transaction actually
            // succeeds. We get an error here when we retry the tx and the second
            // time around it fails. Normally, retries are handled by nonces, but we
            // forget the nonce when we delete a key!
```
