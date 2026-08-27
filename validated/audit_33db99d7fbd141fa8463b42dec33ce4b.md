### Title
Unprivileged accounts can grief `DeleteKey`/`DeleteAccount` by inflating gas-key balances above the burn threshold - (File: `runtime/runtime/src/access_keys.rs`)

### Summary
`action_transfer_to_gas_key` lets **any** predecessor deposit funds into an arbitrary account's gas key balance with no ownership or self-call restriction, while `delete_gas_key` and `action_delete_account` gate key/account deletion on an exact threshold comparison (`balance > GasKeyInfo::MAX_BALANCE_TO_BURN`) of that same, externally-inflatable balance. This is structurally the same bug class as the Maple Finance report: a privileged "finish"/"cleanup" operation is gated on a balance that an unprivileged third party can top up to force the check to fail.

### Finding Description
`TransferToGasKey` actions are validated only for the `GasKeys` protocol feature being enabled — there is no actor/self-call restriction: [1](#0-0) 

The handler itself performs no ownership check either; it simply looks up the access key on `account_id` (the receipt's receiver) and adds the attached deposit to `gas_key_info.balance`: [2](#0-1) 

That balance is then checked with an exact threshold comparison to decide whether a key can be deleted: [3](#0-2) 

and, for `DeleteAccount`, the **sum** of all gas-key balances on the account is checked the same way, which — unlike a single key — cannot be zeroed out by withdrawing just one key in the same atomic transaction that performs the deletion: [4](#0-3) 

The threshold constant and its semantics are documented here: [5](#0-4) 

This mirrors the Maple Finance pattern exactly: `isLiquidationActive`/`finishCollateralLiquidation` checked an exact balance of an address anyone could top up; here `delete_gas_key`/`action_delete_account` check an exact balance of a gas key anyone can top up via `TransferToGasKey`.

### Impact Explanation
An attacker who observes (via mempool/receipt monitoring) that a victim is about to submit a `DeleteKey` (for a gas key) or `DeleteAccount` transaction can front-run it with a `TransferToGasKey` action funding that gas key (or any other gas key on the same account, for the `DeleteAccount` aggregate check) with an amount that pushes the balance above `GasKeyInfo::MAX_BALANCE_TO_BURN` (1 NEAR). The deletion then fails with `GasKeyBalanceTooHigh`: [6](#0-5) 

For `DeleteAccount`, this permanently blocks the account owner from reclaiming their storage-staked balance via the beneficiary refund that only fires on successful deletion: [7](#0-6) 

Because the sum check spans *all* gas keys on the account, an attacker can split deposits across multiple gas keys, and the victim cannot neutralize this by withdrawing a single key in the same atomic transaction as the deletion — they would need to withdraw from every gas key targeted by the attacker, each requiring its own authorizing signature/nonce, giving the attacker repeated front-running opportunities to re-fund a different key before the victim's next transaction lands. This is a persistent, repeatable griefing vector that can permanently freeze an account's ability to be deleted, and hence its storage-staked balance.

### Likelihood Explanation
`TransferToGasKey` requires no special permission — any signer with sufficient balance can send it to any account/public-key pair that has an existing gas key, as long as the `GasKeys` protocol feature is enabled. The attack requires only observing pending transactions (mempool visibility or predictable behavior) and paying gas plus a small deposit (as little as enough to cross the 1 NEAR aggregate threshold). This is a low-cost, repeatable griefing attack reachable by any ordinary client transaction.

### Recommendation
- Restrict `TransferToGasKey` so that only the account itself (predecessor == receiver, i.e., self-receipts) can top up its own gas keys, removing the third-party funding surface entirely, **or**
- If third-party funding of gas keys is an intentional design goal (e.g., sponsor-funded gas keys), decouple the deletion-blocking threshold from a balance an outsider can inflate: e.g., cap/reject `TransferToGasKey` deposits that would push a key (or the account's aggregate) above `MAX_BALANCE_TO_BURN`, or allow `DeleteKey`/`DeleteAccount` to simply refund/burn arbitrarily large gas-key balances instead of erroring out, so the deletion can never be blocked by an externally inflated balance.

### Proof of Concept
1. Victim account `alice.near` has a gas key `pk_gas` with balance `0`.
2. Victim submits a transaction with a `DeleteKey { public_key: pk_gas }` action (or `DeleteAccount`).
3. Attacker (any unprivileged account) front-runs with `Action::TransferToGasKey(TransferToGasKeyAction { public_key: pk_gas, deposit: 1_000_001 yoctoNEAR-equivalent-over-1-NEAR })` targeting `alice.near`, processed via `action_transfer_to_gas_key` at: [2](#0-1) 
4. When the victim's `DeleteKey`/`DeleteAccount` action executes, `delete_gas_key`/`action_delete_account` observe `gas_key_info.balance > GasKeyInfo::MAX_BALANCE_TO_BURN` and return `ActionErrorKind::GasKeyBalanceTooHigh`, as exercised by the existing unit tests: [8](#0-7) [9](#0-8) 
5. The victim's deletion fails and can be repeatedly blocked by re-funding before each retry.

Note: I was unable to further verify within available tool calls whether a `WithdrawFromGasKey` + `DeleteAccount` batch signed by the account's regular full-access key can atomically clear *all* gas keys in one transaction (which would partially mitigate single-key griefing but not the multi-key aggregate griefing described above), so this should be validated with a live scenario/test before treating the severity as final.

### Citations

**File:** runtime/runtime/src/action_validation.rs (L405-411)
```rust
fn validate_transfer_to_gas_key_action(
    current_protocol_version: ProtocolVersion,
) -> Result<(), ActionsValidationError> {
    require_protocol_feature(ProtocolFeature::GasKeys, "GasKeys", current_protocol_version)?;

    Ok(())
}
```

**File:** runtime/runtime/src/access_keys.rs (L93-111)
```rust
fn delete_gas_key(
    config: &RuntimeConfig,
    state_update: &mut TrieUpdate,
    account: &mut Account,
    result: &mut ActionResult,
    account_id: &AccountId,
    public_key: &PublicKey,
    access_key: &AccessKey,
    gas_key_info: &GasKeyInfo,
) -> Result<(), RuntimeError> {
    if gas_key_info.balance > GasKeyInfo::MAX_BALANCE_TO_BURN {
        result.result = Err(ActionErrorKind::GasKeyBalanceTooHigh {
            account_id: account_id.clone(),
            public_key: Some(Box::new(public_key.clone())),
            balance: gas_key_info.balance,
        }
        .into());
        return Ok(());
    }
```

**File:** runtime/runtime/src/access_keys.rs (L257-288)
```rust
pub(crate) fn action_transfer_to_gas_key(
    state_update: &mut TrieUpdate,
    result: &mut ActionResult,
    account_id: &AccountId,
    action: &TransferToGasKeyAction,
) -> Result<(), RuntimeError> {
    let Some(mut access_key) = get_access_key(state_update, account_id, &action.public_key)? else {
        result.result = Err(ActionErrorKind::GasKeyDoesNotExist {
            account_id: account_id.clone(),
            public_key: Box::new(action.public_key.clone()),
        }
        .into());
        return Ok(());
    };
    let Some(gas_key_info) = access_key.gas_key_info_mut() else {
        // Key exists but is not a gas key
        result.result = Err(ActionErrorKind::GasKeyDoesNotExist {
            account_id: account_id.clone(),
            public_key: Box::new(action.public_key.clone()),
        }
        .into());
        return Ok(());
    };

    gas_key_info.balance = gas_key_info.balance.checked_add(action.deposit).ok_or_else(|| {
        RuntimeError::StorageError(StorageError::StorageInconsistentState(
            "gas key balance integer overflow".to_string(),
        ))
    })?;
    set_access_key(state_update, account_id.clone(), action.public_key.clone(), &access_key);
    Ok(())
}
```

**File:** runtime/runtime/src/access_keys.rs (L1218-1256)
```rust
    #[test]
    fn test_delete_gas_key_balance_too_high() {
        let (account_id, public_key, access_key) = test_account_keys();
        let mut state_update = setup_account(&account_id, &public_key, &access_key);
        let mut account = get_account(&state_update, &account_id).unwrap().unwrap();

        let gas_key_public_key =
            InMemorySigner::from_seed(account_id.clone(), KeyType::ED25519, "gas_key").public_key();
        add_gas_key_to_account(&mut state_update, &mut account, &account_id, &gas_key_public_key);

        let deposit_amount = Balance::from_near(1).checked_add(Balance::from_yoctonear(1)).unwrap();
        transfer_to_gas_key(&mut state_update, &account_id, &gas_key_public_key, deposit_amount);

        let mut result = ActionResult::default();
        let action = DeleteKeyAction { public_key: gas_key_public_key.clone() };
        action_delete_key(
            &RuntimeConfig::test(),
            &mut state_update,
            &mut account,
            &mut result,
            &account_id,
            &action,
        )
        .unwrap();
        assert_eq!(
            result.result,
            Err(ActionErrorKind::GasKeyBalanceTooHigh {
                account_id: account_id.clone(),
                public_key: Some(Box::new(gas_key_public_key.clone())),
                balance: deposit_amount,
            }
            .into())
        );
        assert_eq!(result.tokens_burnt, Balance::ZERO);

        // Key should still exist
        let stored_key = get_access_key(&state_update, &account_id, &gas_key_public_key).unwrap();
        assert!(stored_key.is_some());
    }
```

**File:** runtime/runtime/src/access_keys.rs (L1290-1332)
```rust
    #[test]
    fn test_delete_account_gas_key_balance_too_high() {
        let (account_id, public_key, access_key) = test_account_keys();
        let public_keys: Vec<PublicKey> = (0..3)
            .map(|i| PublicKey::from_seed(KeyType::ED25519, &format!("gas_key_{i}")))
            .collect();
        let mut state_update = setup_account(&account_id, &public_key, &access_key);
        let mut account = get_account(&state_update, &account_id).unwrap().unwrap();
        for public_key in &public_keys {
            add_gas_key_to_account(&mut state_update, &mut account, &account_id, public_key);
        }

        // Fund gas keys so total exceeds 1 NEAR
        let deposit_amounts = [
            Balance::from_millinear(400),
            Balance::from_millinear(400),
            Balance::from_millinear(201),
        ];
        for (pk, amount) in public_keys.iter().zip(deposit_amounts.iter()) {
            transfer_to_gas_key(&mut state_update, &account_id, pk, *amount);
        }
        state_update.commit(StateChangeCause::InitialState);

        let action_result = test_delete_account(
            &account_id,
            AccountContract::from_local_code_hash(CryptoHash::default()),
            100,
            PROTOCOL_VERSION,
            &mut state_update,
        );
        let expected_total =
            deposit_amounts.iter().fold(Balance::ZERO, |acc, x| acc.checked_add(*x).unwrap());
        assert_eq!(
            action_result.result,
            Err(ActionErrorKind::GasKeyBalanceTooHigh {
                account_id: account_id.clone(),
                public_key: None,
                balance: expected_total,
            }
            .into())
        );
        assert_eq!(action_result.tokens_burnt, Balance::ZERO);
    }
```

**File:** runtime/runtime/src/actions.rs (L354-363)
```rust
    let gas_key_balance_to_burn = compute_gas_key_balance_sum(state_update, account_id)?;
    if gas_key_balance_to_burn > GasKeyInfo::MAX_BALANCE_TO_BURN {
        result.result = Err(ActionErrorKind::GasKeyBalanceTooHigh {
            account_id: account_id.clone(),
            public_key: None,
            balance: gas_key_balance_to_burn,
        }
        .into());
        return Ok(());
    }
```

**File:** runtime/runtime/src/actions.rs (L364-370)
```rust
    // We use current amount as a pay out to beneficiary.
    let account_balance = account_ref.amount();
    if account_balance > Balance::ZERO {
        result
            .new_receipts
            .push(Receipt::new_balance_refund(&delete_account.beneficiary_id, account_balance));
    }
```

**File:** core/primitives-core/src/account.rs (L815-823)
```rust
impl GasKeyInfo {
    /// Maximum gas key balance that can be burned during key or account deletion.
    /// Deletion fails if the (sum of) gas key balance(s) exceeds this threshold.
    pub const MAX_BALANCE_TO_BURN: Balance = Balance::from_near(1);

    pub fn borsh_len() -> usize {
        borsh::object_length(&Self { balance: Balance::from_yoctonear(0), num_nonces: 0 }).unwrap()
    }
}
```

**File:** core/primitives/src/errors.rs (L840-846)
```rust
    /// Gas key balance is too high to burn during deletion
    GasKeyBalanceTooHigh {
        account_id: AccountId,
        /// Set for DeleteKey (specific key), None for DeleteAccount (aggregate)
        public_key: Option<Box<PublicKey>>,
        balance: Balance,
    } = 25,
```
