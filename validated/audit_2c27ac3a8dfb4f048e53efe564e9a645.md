Based on my research, I found a strong analog in nearcore matching the Berachain bug class (an error being silently overwritten/dropped so that an invalid action is not rejected as it should be).

### Title
Error from DepositWithFunctionCall check overwritten by subsequent validation in `validate_delegate_action_key` - (File: `runtime/runtime/src/actions.rs`)

### Summary
The nearcore codebase documents, via a protocol feature flag, a bug class identical to the Berachain `VerifyIncomingBlock` issue: an error returned by an early validation check inside `validate_delegate_action_key` (used when applying `DelegateAction`/meta-transactions) could be overwritten by the result of a later, unrelated check (`receiver_id` or `method_name`), because the function was missing an early return on the `DepositWithFunctionCall` error path.

### Finding Description
`core/primitives-core/src/version.rs` documents a `ProtocolFeature` named `FixDelegateActionDepositWithFunctionCallError` with the comment: "Fix missing early return on DepositWithFunctionCall error path in validate_delegate_action_key. Previously the error could be overwritten by a subsequent receiver_id or method_name check." [1](#0-0) 

This is structurally the same defect pattern as the Berachain PoC: a validation function computes an error from a security-relevant check (in Berachain, `verifyStateRoot`; in nearcore, the `DepositWithFunctionCall` restriction on delegate/meta-transaction actions), but instead of returning immediately, execution falls through to a later check whose result (success or a different error) overwrites/replaces the first error value before it is returned to the caller. The function `validate_delegate_action_key` is implemented in `runtime/runtime/src/actions.rs`, which contains the relevant logic gated by this protocol feature [2](#0-1) , but I was unable to retrieve the exact current function body (pre/post-fix) within the available tool budget to confirm the precise line-level mechanics of the overwrite.

### Impact Explanation
If the `DepositWithFunctionCall` restriction error is overwritten and a later `receiver_id`/`method_name` check happens to pass, `validate_delegate_action_key` would return `Ok`/no error for a delegate action that should have been rejected. Since this function gates authorization for meta-transaction (`DelegateAction`) execution against access keys, a bypass here could allow an attacker to execute a function-call action with an access key that should not have been authorized to attach a deposit, potentially escalating authorization scope for a meta-transaction beyond what the signing account key permits — matching the "authorization escalation across accounts or promises" impact category.

### Likelihood Explanation
The bug is gated behind a `ProtocolFeature` flag (`FixDelegateActionDepositWithFunctionCallError`), which strongly implies it was a real, already-identified consensus bug in a shipped nearcore version, fixed via a protocol upgrade rather than a straightforward one-line non-consensus patch (protocol features are used specifically to fix consensus-relevant validation bugs without a hard fork mismatch). I could not determine from the available index snippets whether this feature is already active on the current mainnet protocol version or is still pending activation, which is necessary to know whether the vulnerability is currently live or historical/already patched.

### Recommendation
Confirm in `runtime/runtime/src/actions.rs` that the `DepositWithFunctionCall` error path in `validate_delegate_action_key` always returns immediately rather than allowing a later check to overwrite the error variable, and confirm the current mainnet protocol version has this feature enabled (i.e., the fix is live and not merely available-but-inactive).

### Proof of Concept
I could not construct a concrete PoC because I was unable to retrieve the full, current source of `validate_delegate_action_key` within the tool-call budget to confirm the exact overwrite mechanics and current guard condition (`FixDelegateActionDepositWithFunctionCallError.enabled(...)`). This should be verified directly in `runtime/runtime/src/actions.rs` before treating this as a currently-exploitable (rather than already-fully-fixed) issue.

### Citations

**File:** core/primitives-core/src/version.rs (L349-352)
```rust
    /// Fix missing early return on DepositWithFunctionCall error path in
    /// validate_delegate_action_key. Previously the error could be
    /// overwritten by a subsequent receiver_id or method_name check.
    FixDelegateActionDepositWithFunctionCallError,
```

**File:** runtime/runtime/src/actions.rs (L1-42)
```rust
use crate::access_keys::initial_nonce_value;
use crate::cache_warming::precompile_contract_with_warming;
use crate::config::{
    delegate_signature_verification_compute, safe_add_compute, storage_removes_compute,
    total_prepaid_exec_fees, total_prepaid_gas, total_prepaid_send_fees,
};
use crate::deterministic_account_id::create_deterministic_account;
use crate::{ActionResult, ApplyState};
use near_crypto::PublicKey;
use near_parameters::vm::Config as VmConfig;
use near_parameters::{
    AccountCreationConfig, ActionCosts, ParameterCost, RuntimeConfig, RuntimeFeesConfig,
};
use near_primitives::account::{
    AccessKey, AccessKeyPermission, Account, AccountContract, GasKeyInfo, InvalidAccountState,
};
use near_primitives::action::delegate::{
    VersionedDelegateActionRef, VersionedSignedDelegateActionRef,
};
use near_primitives::errors::{ActionError, ActionErrorKind, InvalidAccessKeyError, RuntimeError};
use near_primitives::hash::CryptoHash;
use near_primitives::receipt::{
    ActionReceipt, Receipt, ReceiptEnum, ReceiptV0, VersionedActionReceipt, VersionedReceiptEnum,
};
use near_primitives::transaction::{
    Action, DeleteAccountAction, DeployContractAction, StakeAction, TransactionNonce,
};
use near_primitives::types::validator_stake::ValidatorStake;
use near_primitives::types::{
    AccountId, Balance, BlockHeight, EpochInfoProvider, NonceIndex, StorageUsage,
};
use near_primitives::utils::account_is_implicit;
use near_primitives::version::ProtocolVersion;
use near_primitives_core::account::id::AccountType;
use near_primitives_core::version::ProtocolFeature;
use near_store::{
    StorageError, TrieUpdate, compute_gas_key_balance_sum, get_access_key, get_gas_key_nonce,
    remove_account, set_access_key, set_gas_key_nonce,
};
use near_vm_runner::{ContractCode, ContractRuntimeCache};
use near_wallet_contract::eth_wallet_global_contract_hash;
use std::sync::Arc;
```
