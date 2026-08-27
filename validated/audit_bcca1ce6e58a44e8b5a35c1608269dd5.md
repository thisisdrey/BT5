### Title
Attached NEAR deposit is permanently stranded in the eth-implicit Wallet Contract when `rlp_execute` fails before a promise is created - (File: `runtime/near-wallet-contract/implementation/wallet-contract/src/lib.rs`)

### Summary
`WalletContract::rlp_execute` is `#[payable]` and accepts an attached NEAR deposit from an external, unprivileged caller (a relayer submitting a signed Ethereum-style transaction on behalf of the wallet owner). The contract explicitly tracks this deposit in a `CallerDeposit` struct specifically so it can be refunded to the caller if the requested cross-contract action later fails. However, several early error paths inside `inner_rlp_execute`/`rlp_execute` return an error (or a synchronous value) *before* the `caller_deposit` is ever threaded into a promise/callback, silently dropping it. In those code paths the attached deposit is never refunded and permanently remains in the wallet contract's balance.

### Finding Description
`inner_rlp_execute` computes `caller_deposit` from the attached deposit at the very top of the function: [1](#0-0) 

`CallerDeposit::new` is documented as existing specifically "to refund the caller's deposit if the cross-contract call fails": [2](#0-1) 

The only place this refund is actually issued is inside `rlp_execute_callback`, on a failed promise result: [3](#0-2) 

But `caller_deposit` only reaches `rlp_execute_callback` if `inner_rlp_execute` successfully builds and chains a `Promise`. Several error branches instead bail out with `Err(err)` (or, for the "relayer" family of errors, with `Ok`-less flow that never touches `caller_deposit` at all), and the caller_deposit variable is simply dropped without ever creating a refund transfer: [4](#0-3) 

Back in `rlp_execute`, any such `Err` (other than the specific `Error::Relayer` + "signer is the wallet itself" combination that triggers `create_ban_relayer_promise`) is converted directly into a synchronous `PromiseOrValue::Value(e.into())`: [5](#0-4) 

Because `rlp_execute` is `#[payable]`, the attached deposit was already credited to the wallet contract's account balance as part of executing the `FunctionCall` action, regardless of whether the method logic later returns a value or a promise. When the method takes the synchronous-error path (e.g. `Error::User`, `Error::AccountId`, or an `Error::Relayer` from an external relayer account, i.e. `env::signer_account_id() != current_account_id`), no promise is scheduled to send any part of that deposit back to `caller_deposit.account_id`. The deposit is simply absorbed by the wallet contract with no code path that ever refunds it.

This is structurally the same bug class as the reported WatchPug finding: the contract has a defined/expected refund mechanism for unused/failed-path funds (ETH refund in the Solidity `Swap.sol` case; `CallerDeposit` refund in the Wallet Contract case), but only a subset of the failure paths actually perform the refund, so leftover value is left stuck in the contract.

### Impact Explanation
An external relayer (an ordinary, unprivileged caller — not the wallet owner) that attaches NEAR to `rlp_execute` (e.g. to cover an emulated base-token/ERC-20 relayer fee or a cross-contract call deposit) can have that entire attached deposit permanently retained by the wallet contract if the transaction hits one of the early, non-promise error paths (malformed/unsupported action, invalid account id, or a relayer-classified parsing error from a non-owner signer). Since the deposit becomes indistinguishable general-purpose balance of the wallet contract, it is a permanent loss of the caller's funds ("leak value" scenario, matching the Med-severity classification of the analogous report), not a temporary freeze.

### Likelihood Explanation
The eth-implicit Wallet Contract is a production, in-scope contract that any relayer can call permissionlessly with `rlp_execute`. Triggering one of the affected `Err` paths only requires submitting a syntactically-invalid or unsupported RLP-encoded Ethereum transaction (e.g., referencing `AddFullAccessKey`, an unparsable account id, or another `Error::User`/`Error::AccountId`/non-owner `Error::Relayer` condition) while attaching a non-zero deposit — no special privileges or races are required, only ordinary transaction submission.

### Recommendation
Ensure every early-return error path in `inner_rlp_execute` (and the corresponding handling in `rlp_execute`) that already computed a non-empty `caller_deposit` issues a refund `Promise::new(caller_deposit.account_id).transfer(...)` before returning, mirroring the logic already present in `rlp_execute_callback`. Alternatively, restructure the function so `caller_deposit` refund handling is centralized and cannot be bypassed by any error branch.

### Proof of Concept
1. An external relayer account `R` (≠ wallet's own account) calls `wallet.rlp_execute(target, tx_bytes_b64)` attaching a deposit `D > 0`, where `tx_bytes_b64` encodes a transaction that decodes to an unsupported/invalid action or triggers an `Error::User`/`Error::AccountId`/non-owner `Error::Relayer` result inside `internal::parse_rlp_tx_to_action` (e.g. an `AddKey` with `FullAccess` permission, per `UnsupportedAction::AddFullAccessKey`, or a malformed `target`/`to` address combination).
2. `inner_rlp_execute` computes `caller_deposit = Some(CallerDeposit { account_id: R, yocto_near: D })` at line 345, then hits the `Err(err)` branch at lines 389-409 and returns `Err(err)` without ever using `caller_deposit`.
3. `rlp_execute` receives this `Err`, falls to the final arm `Err(e) => PromiseOrValue::Value(e.into())` (line 126), and returns synchronously with no promise scheduled.
4. The deposit `D` attached by `R` remains part of the wallet contract's on-chain balance; `R` never receives any transfer back. Repeating this is possible any time R is willing to sacrifice `D`, confirming deterministic, permanent loss of the attacker/victim's own attached funds with no compensating refund path.

### Citations

**File:** runtime/near-wallet-contract/implementation/wallet-contract/src/lib.rs (L116-127)
```rust
        match result {
            Ok(promise) => {
                self.has_in_flight_tx = true;
                PromiseOrValue::Promise(promise)
            }
            Err(Error::Relayer(_)) if env::signer_account_id() == current_account_id => {
                let promise = create_ban_relayer_promise(current_account_id);
                self.has_in_flight_tx = true;
                PromiseOrValue::Promise(promise)
            }
            Err(e) => PromiseOrValue::Value(e.into()),
        }
```

**File:** runtime/near-wallet-contract/implementation/wallet-contract/src/lib.rs (L296-317)
```rust
        match env::promise_result(0) {
            PromiseResult::Failed => {
                // The cross-contract call failed, refund the caller if needed
                if let Some(CallerDeposit { account_id, yocto_near }) = caller_deposit {
                    let refund_promise = env::promise_batch_create(&account_id);
                    env::promise_batch_action_transfer(
                        refund_promise,
                        NearToken::from_yoctonear(yocto_near.into()),
                    );
                }

                ExecuteResponse {
                    success: false,
                    success_value: None,
                    error: Some("Failed Near promise".into()),
                }
            }
            PromiseResult::Successful(value) => {
                ExecuteResponse { success: true, success_value: Some(value), error: None }
            }
        }
    }
```

**File:** runtime/near-wallet-contract/implementation/wallet-contract/src/lib.rs (L340-345)
```rust
    let context = ExecutionContext::new(
        current_account_id.clone(),
        predecessor_account_id,
        env::attached_deposit(),
    )?;
    let caller_deposit = CallerDeposit::new(&context);
```

**File:** runtime/near-wallet-contract/implementation/wallet-contract/src/lib.rs (L389-409)
```rust
        Err(err @ Error::User(_)) => {
            // Increment nonce on all user errors to prevent replay.
            *nonce = nonce.saturating_add(1);
            return Err(err);
        }
        Err(err) => {
            // Do not increment nonce on Relayer or AccountId errors.
            // The latter error is an issue in the deployment (so the nonce is meaningless).
            // The former arises from the relayer itself doing something wrong and thus the
            // user's transaction could still be valid and potentially submitted properly by
            // another relayer. To allow this we do not increment the nonce.
            //
            // Note: if a relayer is using an access key for this wallet then that key will
            // still be revoked (in the main logic of `rlp_execute`). This fact together with
            // the condition that there only be one in-flight transaction at a time implies
            // that a relayer cannot maliciously burn a large portion of the user's tokens.
            // If the relayer is not using an access key then they are spending their own
            // resources on the gas and therefore we do not care if the relayer submits
            // the same faulty transaction multiple times.
            return Err(err);
        }
```

**File:** runtime/near-wallet-contract/implementation/wallet-contract/src/types.rs (L172-191)
```rust
/// A data type to keep track of the deposit given by an external caller.
/// This allows us to refund the caller's deposit if the cross-contract call fails.
#[derive(Debug, PartialEq, Eq, Clone, serde::Serialize, serde::Deserialize)]
pub struct CallerDeposit {
    pub account_id: AccountId,
    pub yocto_near: NonZeroU128,
}

impl CallerDeposit {
    pub fn new(context: &ExecutionContext) -> Option<Self> {
        // Only track for external (non-self) callers
        if context.current_account_id == context.predecessor_account_id {
            return None;
        }

        NonZeroU128::new(context.attached_deposit.as_yoctonear()).map(|yocto_near| Self {
            account_id: context.predecessor_account_id.clone(),
            yocto_near,
        })
    }
```
