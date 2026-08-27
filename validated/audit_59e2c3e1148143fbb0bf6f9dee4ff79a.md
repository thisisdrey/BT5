## Title
DeployGlobalContract in AccountId mode lacks a same-account (predecessor==receiver) restriction, allowing any receipt to overwrite the global contract identity owned by an unrelated account - (File: `runtime/runtime/src/global_contracts.rs`)

### Summary
The external report describes a UUPS proxy where `upgradeToAndCall()` is missing the `onlyUpgrader` authorization check that `upgradeTo()` enforces, letting any address change the contract implementation. The closest reachable analog in nearcore's transaction/action-execution surface is the `DeployGlobalContract` action with `GlobalContractDeployMode::AccountId`, which is nearcore's mechanism for "upgradeable" contract code referenced by an account identifier (any account can later reference the code via `UseGlobalContract(AccountId(owner))`, and the owner can push new code to update it for all users) — conceptually parallel to an upgradeable proxy's implementation slot.

### Finding Description
`docs/RuntimeSpec/Actions.md` enumerates the actions that require `predecessor_id == receiver_id` (i.e., can only be executed by the account itself, analogous to `onlyUpgrader`/self-authorization): `DeployContract`, `Stake`, `AddKey`, `DeleteKey`, `DeleteAccount` [1](#0-0) . `DeployGlobalContract` is conspicuously **not** in that list, mirroring the pattern in the report where one entry point (`upgradeTo`) is protected but a sibling entry point (`upgradeToAndCall`) is not.

`action_deploy_global_contract` in `runtime/runtime/src/global_contracts.rs` takes the executing account (`account_id`, i.e. the receipt's `receiver_id`) and, for `GlobalContractDeployMode::AccountId`, builds the global contract's permanent identity directly from that `account_id` via `initiate_distribution`, without any check that the predecessor (the caller who added this action to the receipt) is the same as that account: [2](#0-1)  and [3](#0-2) .

`validate_action_with_mode` dispatches `Action::DeployGlobalContract` to `validate_deploy_global_contract_action`, which — unlike `Action::DeployContract`, `Action::Stake`, `Action::AddKey`, `Action::DeleteKey` — has no equivalent guard comparing `receiver` against a required actor: [4](#0-3) . This action's storage cost is paid out of whatever account is the current `account`/`receiver` of the receipt, not necessarily the account that added the action to the receipt.

I was not able to fully verify (within available tool budget) whether an additional guard exists elsewhere in the code (e.g., in the FunctionCall promise-batch host functions in `near-vm-runner`) that restricts `promise_batch_action_deploy_global_contract` so that a contract can only add this action to a promise whose receiver is `current_account_id()` (self). If such a restriction exists at the host-function layer, it would fully mitigate a cross-account attack. This uncertainty means the finding should be treated as a lead requiring confirmation against `runtime/near-vm-runner/src/logic/logic.rs` (`promise_batch_action_deploy_global_contract`) before being considered conclusively exploitable.

### Impact Explanation
If confirmed (i.e., if no receiver==self restriction exists at the host-function or receipt-validation layer), an arbitrary contract that a victim account has no relationship with could, upon receiving any FunctionCall receipt, add a `DeployGlobalContract(deploy_mode = AccountId)` action targeting the victim's account as receiver. This would (a) burn the victim's own balance to pay `global_contract_storage_amount_per_byte`, and (b) associate malicious code with `GlobalContractIdentifier::AccountId(victim)`, corrupting the "upgrade slot" that any downstream account referencing `UseGlobalContract(AccountId(victim))` would subsequently execute — a direct authorization-escalation/state-corruption analog to the reported "anyone can upgrade the implementation" bug.

### Likelihood Explanation
This is a Medium-confidence lead, not a proven vulnerability, because the analysis was cut off before confirming or ruling out an existing receiver==self check enforced at the host-function/promise-creation layer (`near-vm-runner`) or elsewhere in receipt validation. Given nearcore explicitly enforces `predecessor_id == receiver_id` for every other action that mutates an account's privileged state (contract code, keys, stake, deletion), it would be a significant and easily-testable gap if `DeployGlobalContract` truly lacked the same restriction.

### Recommendation
Add an explicit `predecessor_id == receiver_id` (self-only) requirement for `Action::DeployGlobalContract` when `deploy_mode == GlobalContractDeployMode::AccountId`, either in `validate_action_with_mode`/`validate_deploy_global_contract_action` (`runtime/runtime/src/action_validation.rs`) or in `action_deploy_global_contract` (`runtime/runtime/src/global_contracts.rs`), consistent with the restriction already applied to `DeployContract`, `Stake`, `AddKey`, `DeleteKey`, and `DeleteAccount`.

### Proof of Concept
Not executed — this requires confirming, via a test similar to `test_global_contract_update` in `test-loop-tests/src/tests/global_contracts.rs`, whether a contract account B can successfully add a `DeployGlobalContract(deploy_mode=AccountId)` action to a promise whose receiver is a different, unrelated account C, using C's balance and identifier, without any access-key authorization from C. This was not verified in the available search budget and should be confirmed by a background agent with access to the full `near-vm-runner` promise-creation code before treating this as a confirmed vulnerability. [5](#0-4)

### Citations

**File:** docs/RuntimeSpec/Actions.md (L26-32)
```markdown
For the following actions, `predecessor_id` and `receiver_id` are required to be equal:

- `DeployContract`
- `Stake`
- `AddKey`
- `DeleteKey`
- `DeleteAccount`
```

**File:** runtime/runtime/src/global_contracts.rs (L24-62)
```rust
pub(crate) fn action_deploy_global_contract(
    state_update: &mut TrieUpdate,
    account: &mut Account,
    account_id: &AccountId,
    apply_state: &ApplyState,
    deploy_contract: &DeployGlobalContractAction,
    result: &mut ActionResult,
) -> Result<(), RuntimeError> {
    let _span = tracing::debug_span!(target: "runtime", "action_deploy_global_contract").entered();

    let storage_cost = apply_state
        .config
        .fees
        .storage_usage_config
        .global_contract_storage_amount_per_byte
        .saturating_mul(deploy_contract.code.len() as u128);
    let Some(updated_balance) = account.amount().checked_sub(storage_cost) else {
        result.result = Err(ActionErrorKind::LackBalanceForState {
            account_id: account_id.clone(),
            amount: storage_cost,
        }
        .into());
        return Ok(());
    };
    result.tokens_burnt =
        result.tokens_burnt.checked_add(storage_cost).ok_or(IntegerOverflowError)?;
    account.set_amount(updated_balance);

    initiate_distribution(
        state_update,
        account_id.clone(),
        deploy_contract.code.clone(),
        &deploy_contract.deploy_mode,
        apply_state.shard_id,
        result,
    )?;

    Ok(())
}
```

**File:** runtime/runtime/src/global_contracts.rs (L142-170)
```rust
fn initiate_distribution(
    state_update: &mut TrieUpdate,
    account_id: AccountId,
    contract_code: Arc<[u8]>,
    deploy_mode: &GlobalContractDeployMode,
    current_shard_id: ShardId,
    result: &mut ActionResult,
) -> Result<(), RuntimeError> {
    let id = match deploy_mode {
        GlobalContractDeployMode::CodeHash => {
            GlobalContractIdentifier::CodeHash(hash(&contract_code))
        }
        GlobalContractDeployMode::AccountId => {
            GlobalContractIdentifier::AccountId(account_id.clone())
        }
    };
    // Increment the nonce and write it to state immediately to prevent multiple
    // distributions with the same nonce from being initiated. This requires
    // allowing the same nonce in the freshness check when applying the
    // distribution receipt.
    let nonce = increment_nonce(state_update, &id)?;
    let distribution_receipt =
        GlobalContractDistributionReceipt::new(id, current_shard_id, vec![], contract_code, nonce);
    let distribution_receipts =
        Receipt::new_global_contract_distribution(account_id, distribution_receipt);
    // No need to set receipt_id here, it will be generated as part of apply_action_receipt
    result.new_receipts.push(distribution_receipts);
    Ok(())
}
```

**File:** runtime/runtime/src/action_validation.rs (L136-180)
```rust
    match action {
        Action::CreateAccount(_) => Ok(()),
        Action::DeployContract(a) => validate_deploy_contract_action(limit_config, a),
        Action::DeployGlobalContract(a) => validate_deploy_global_contract_action(limit_config, a),
        Action::UseGlobalContract(a) => validate_use_global_contract_action(a),
        Action::FunctionCall(a) => {
            validate_function_call_action(limit_config, a, current_protocol_version, mode)
        }
        Action::Transfer(_) => Ok(()),
        Action::Stake(a) => validate_stake_action(a),
        Action::AddKey(a) => validate_add_key_action(limit_config, a, current_protocol_version),
        Action::DeleteKey(_) => Ok(()),
        Action::DeleteAccount(a) => validate_delete_action(a),
        Action::Delegate(a) => validate_delegate_action(
            limit_config,
            (&a.delegate_action).into(),
            receiver,
            current_protocol_version,
            mode,
        ),
        Action::DelegateV2(a) => {
            require_protocol_feature(
                ProtocolFeature::DelegateV2,
                "DelegateV2",
                current_protocol_version,
            )?;
            validate_delegate_action(
                limit_config,
                (&a.delegate_action).into(),
                receiver,
                current_protocol_version,
                mode,
            )
        }
        Action::DeterministicStateInit(a) => {
            validate_deterministic_state_init(limit_config, a, receiver)
        }
        Action::TransferToGasKey(_) => {
            validate_transfer_to_gas_key_action(current_protocol_version)
        }
        Action::WithdrawFromGasKey(_) => {
            validate_withdraw_from_gas_key_action(current_protocol_version)
        }
    }
}
```

**File:** test-loop-tests/src/tests/global_contracts.rs (L71-107)
```rust
#[test]
fn test_global_contract_update() {
    let mut env = GlobalContractsTestEnv::setup(Balance::from_near(1000));
    let use_accounts = [env.account_shard_0.clone(), env.account_shard_1.clone()];

    env.deploy_trivial_global_contract(GlobalContractDeployMode::AccountId);

    for account in &use_accounts {
        env.use_global_contract(
            account,
            GlobalContractIdentifier::AccountId(env.deploy_account.clone()),
        );

        // Currently deployed trivial contract doesn't have any methods,
        // so we expect any function call to fail with MethodNotFound error
        let call_tx = env.call_global_contract_tx(account.clone(), account.clone());
        let call_outcome = env.execute_tx(call_tx);
        assert_matches!(
            call_outcome.status,
            FinalExecutionStatus::Failure(TxExecutionError::ActionError(ActionError {
                kind: ActionErrorKind::FunctionCallError(FunctionCallError::MethodResolveError(
                    MethodResolveError::MethodNotFound
                )),
                index: _
            }))
        );
    }

    env.deploy_global_contract(GlobalContractDeployMode::AccountId);

    for account in &use_accounts {
        // Function call should be successful after deploying rs contract
        // containing the function we call here
        env.assert_call_global_contract_success(account.clone(), account.clone());
    }
}

```
