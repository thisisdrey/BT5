# No Vulnerability found for this question.

The scenario describes a "metering gap" between per-action size checks and per-receipt aggregate resource accounting for `DeployGlobalContract` actions. This gap does not exist in the code:

1. **Per-action size limit**: `promise_batch_action_deploy_global_contract_impl` enforces `max_contract_size` on each blob before appending the action [1](#0-0) .

2. **Per-receipt deploy-action count cap**: `validate_number_of_deploy_actions`, invoked from `validate_actions_with_mode` for every newly created receipt, counts both `DeployContract` and `DeployGlobalContract` actions together and rejects the receipt if they exceed `max_deploy_actions_per_receipt`, which bounds the total number of deploy actions (and thus the maximum possible cumulative deployed bytes = `max_deploy_actions_per_receipt * max_contract_size`) per receipt [2](#0-1) [3](#0-2) .

3. **Total-action cap**: the same validation also caps the total number of actions per receipt via `max_actions_per_receipt` [4](#0-3) .

4. **Full gas accounting per byte written**: every `DeployGlobalContract` action pays both a base fee and a per-byte dispatch/execution fee proportional to `code_len` via `pay_action_base`/`pay_action_per_byte`, so the cumulative gas burnt scales linearly with the total bytes across all deploy actions in the batch, not just per individual action [5](#0-4) . If the attacker hasn't attached enough gas to cover the cumulative per-byte cost of all the deploy actions, the gas counter fails the call with an out-of-gas error before any writes reach the trie.

Given these three independent, already-existing bounds (per-action size cap, per-receipt deploy-action count cap, and gas metering that scales with total bytes written), an attacker cannot force the runtime to write more global-contract bytes than what is accounted for and gas-charged. The scenario's premise — that no per-receipt aggregate cap exists — is factually incorrect for this codebase, and the test in [6](#0-5)  demonstrates the cap is actively enforced and tested for mixed `DeployContract`/`DeployGlobalContract` batches.

### Citations

**File:** runtime/near-vm-runner/src/logic/logic.rs (L2711-2716)
```rust
        let code = get_memory_or_register!(self, code_ptr, code_len)?;
        let code_len = code.len() as u64;
        let limit = self.config.limit_config.max_contract_size;
        if code_len > limit {
            return Err(HostError::ContractSizeExceeded { size: code_len, limit }.into());
        }
```

**File:** runtime/near-vm-runner/src/logic/logic.rs (L2719-2725)
```rust
        let (receipt_idx, sir) = self.promise_idx_to_receipt_idx_with_sir(promise_idx)?;

        self.pay_action_base(ActionCosts::deploy_global_contract_base, sir)?;
        self.pay_action_per_byte(ActionCosts::deploy_global_contract_byte, code_len, sir)?;

        self.ext.append_action_deploy_global_contract(receipt_idx, code, mode);
        Ok(())
```

**File:** runtime/runtime/src/action_validation.rs (L19-36)
```rust
/// Validates that the number of deploy actions in the given list of actions doesn't exceed the limit.
fn validate_number_of_deploy_actions(
    actions: &[Action],
    max_deploy_actions_per_receipt: u64,
) -> Result<(), ActionsValidationError> {
    let deploy_actions_count = actions
        .iter()
        .filter(|a| matches!(a, Action::DeployContract(_) | Action::DeployGlobalContract(_)))
        .count() as u64;
    if deploy_actions_count > max_deploy_actions_per_receipt {
        Err(ActionsValidationError::TotalNumberOfDeployActionsExceeded {
            number_of_deploy_actions: deploy_actions_count,
            limit: max_deploy_actions_per_receipt,
        })
    } else {
        Ok(())
    }
}
```

**File:** runtime/runtime/src/action_validation.rs (L69-74)
```rust
    if actions.len() as u64 > limit_config.max_actions_per_receipt {
        return Err(ActionsValidationError::TotalNumberOfActionsExceeded {
            total_number_of_actions: actions.len() as u64,
            limit: limit_config.max_actions_per_receipt,
        });
    }
```

**File:** runtime/runtime/src/action_validation.rs (L93-95)
```rust
    if mode == ValidateReceiptMode::NewReceipt {
        validate_number_of_deploy_actions(actions, limit_config.max_deploy_actions_per_receipt)?;
    }
```

**File:** runtime/runtime/src/action_validation.rs (L676-732)
```rust
    fn test_validate_actions_num_deploy_actions() {
        let receiver: AccountId = "alice.near".parse().unwrap();
        let deploy_local = || Action::DeployContract(DeployContractAction { code: vec![1; 5] });
        let deploy_global = || {
            Action::DeployGlobalContract(DeployGlobalContractAction {
                code: vec![1; 5].into(),
                deploy_mode: GlobalContractDeployMode::CodeHash,
            })
        };

        let mut limit_config = test_limit_config();
        limit_config.max_deploy_actions_per_receipt = 2;

        // Pure DeployContract over the limit → error.
        assert_eq!(
            validate_actions(
                &limit_config,
                &[deploy_local(), deploy_local(), deploy_local()],
                &receiver,
                PROTOCOL_VERSION,
            )
            .expect_err("expected error"),
            ActionsValidationError::TotalNumberOfDeployActionsExceeded {
                number_of_deploy_actions: 3,
                limit: 2,
            },
        );

        // Pure DeployGlobalContract over the limit → error.
        assert_eq!(
            validate_actions(
                &limit_config,
                &[deploy_global(), deploy_global(), deploy_global()],
                &receiver,
                PROTOCOL_VERSION,
            )
            .expect_err("expected error"),
            ActionsValidationError::TotalNumberOfDeployActionsExceeded {
                number_of_deploy_actions: 3,
                limit: 2,
            },
        );

        // Mixed deploy actions summing over the limit → error.
        assert_eq!(
            validate_actions(
                &limit_config,
                &[deploy_local(), deploy_global(), deploy_local()],
                &receiver,
                PROTOCOL_VERSION,
            )
            .expect_err("expected error"),
            ActionsValidationError::TotalNumberOfDeployActionsExceeded {
                number_of_deploy_actions: 3,
                limit: 2,
            },
        );
```
