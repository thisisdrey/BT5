I found a legitimate analog to the "ETH trapped" bug class in the `near-wallet-contract` (eth-implicit wallet).

### Title
Attached deposit permanently absorbed by wallet contract when RLP transaction parsing/validation fails before promise creation - (File: `runtime/near-wallet-contract/implementation/wallet-contract/src/lib.rs`)

### Summary
`WalletContract::rlp_execute` is `#[payable]` and lets any predecessor (e.g. a relayer submitting a signed Ethereum-style transaction on behalf of an eth-implicit account) attach a NEAR deposit that is meant to be refunded if the requested action ultimately fails. The refund mechanism is implemented via `CallerDeposit`, which is threaded through to `rlp_execute_callback`/`address_check_callback` and refunded only when a downstream cross-contract *promise* fails. However, in `inner_rlp_execute`, the `caller_deposit` value computed from the attached deposit is silently dropped whenever `parse_rlp_tx_to_action` (or the initial nonce check) fails and the function returns `Err(...)` before any promise is ever created.

### Finding Description
In `inner_rlp_execute` (`runtime/near-wallet-contract/implementation/wallet-contract/src/lib.rs:330-410`): [1](#0-0) 
the function first checks the nonce, builds the `ExecutionContext` from `env::attached_deposit()`, and computes `caller_deposit` via `CallerDeposit::new(&context)`, which only tracks the deposit when the predecessor differs from the account owner: [2](#0-1) 

`caller_deposit` is only ever consumed (passed into a callback that can later trigger a refund via `rlp_execute_callback`) in the success branch of `parse_rlp_tx_to_action`: [3](#0-2) 

But in both error branches (`Err(err @ Error::User(_))` and the catch-all `Err(err)`), the function simply returns `Err(err)` — `caller_deposit` is dropped without ever creating a refund promise: [4](#0-3) 

Back in `rlp_execute`, any `Err` returned from `inner_rlp_execute` (other than the `Error::Relayer` self-relay ban case) is converted directly into a `PromiseOrValue::Value(e.into())` with no promise at all: [5](#0-4) 

Because the method call itself does not panic (it returns a value, `ExecuteResponse{success:false,...}`), the enclosing `FunctionCall` action *succeeds* from the runtime's point of view. This means NEAR's built-in deposit-refund mechanism — which only fires when the whole action receipt fails (`result.result.is_err()`), see `refund_unspent_gas_and_deposits` — never triggers: [6](#0-5) 

So the attached deposit is simply merged into the current account's (the eth-implicit wallet's) own balance forever, with no explicit refund and no way for the depositor to reclaim it.

This is exactly the same bug class as the Opyn `Controller` report: a caller attaches value expecting either successful execution or a refund on failure, but a class of failures (early parse/validation errors) bypasses the refund path entirely, permanently trapping the funds in the receiving contract's balance.

### Impact Explanation
Any external, unprivileged predecessor (e.g. a relayer, or any account that is not the wallet owner) who attaches a NEAR deposit to `rlp_execute` — analogous to the `test_caller_refunds` scenario which explicitly demonstrates the intended refund-on-failure guarantee — permanently loses that deposit if the call fails before a promise is created: nonce exhaustion (`AccountNonceExhausted`), any `Error::User` (malformed RLP, `ExcessYoctoNear`, `UnsupportedAction`, invalid signature, nonce mismatch, etc.), or any other early error. The deposit is absorbed into the wallet account's own balance, which benefits the wallet owner at the depositor's expense — a permanent, unrecoverable loss of funds for the caller.

### Likelihood Explanation
This path is trivially reachable by an ordinary user/relayer transaction: submit `rlp_execute` with a deposit and a transaction that fails validation (e.g., a stale/incorrect nonce, a case demonstrated to occur in the existing `test_excess_yocto` and nonce-mismatch tests) — no privileged access or special conditions are required, and nonce races between competing relayers are a normal, expected occurrence in this design (the code's own comments acknowledge multiple relayers may compete to submit the same signed transaction).

### Recommendation
In `inner_rlp_execute`, when `parse_rlp_tx_to_action` (or the nonce check) fails and `caller_deposit` is `Some`, issue a `promise_batch_action_transfer` back to `caller_deposit.account_id` for `caller_deposit.yocto_near` before returning `Err`, mirroring the refund logic already present in `rlp_execute_callback`'s `PromiseResult::Failed` branch.

### Proof of Concept
1. Deploy the wallet contract as a global contract for an eth-implicit account `A` (owned by `wallet_sk`), per `test_wallet_contract_interaction`/`TestContext`.
2. As an external NEAR account `Caller` (not `A`), call `A.rlp_execute(target, tx_bytes_b64)` attaching a deposit (e.g. 3 NEAR), where `tx_bytes_b64` is a validly-signed Ethereum transaction but uses a nonce that does not match `A`'s current nonce (or is otherwise malformed to hit an `Error::User`/`Error::AccountNonceExhausted` path), similar to the setup in `runtime/near-wallet-contract/implementation/wallet-contract/src/tests/sanity.rs:170-229` and `.../tests/user_error.rs:322-356`, but without going through the promise-creation path.
3. Observe that `inner_rlp_execute` returns `Err(...)` at `runtime/near-wallet-contract/implementation/wallet-contract/src/lib.rs:389-409`, `rlp_execute` returns `PromiseOrValue::Value(...)` (no promise), and `Caller`'s deposit is now part of `A`'s balance permanently, with no refund receipt generated and no callback to reclaim it.

### Citations

**File:** runtime/near-wallet-contract/implementation/wallet-contract/src/lib.rs (L108-127)
```rust
        let result = inner_rlp_execute(
            current_account_id.clone(),
            predecessor_account_id,
            target,
            tx_bytes_b64,
            &mut self.nonce,
        );

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

**File:** runtime/near-wallet-contract/implementation/wallet-contract/src/lib.rs (L337-345)
```rust
    if *nonce == u64::MAX {
        return Err(Error::AccountNonceExhausted);
    }
    let context = ExecutionContext::new(
        current_account_id.clone(),
        predecessor_account_id,
        env::attached_deposit(),
    )?;
    let caller_deposit = CallerDeposit::new(&context);
```

**File:** runtime/near-wallet-contract/implementation/wallet-contract/src/lib.rs (L347-409)
```rust
    let parsing_result = internal::parse_rlp_tx_to_action(&tx_bytes_b64, &target, &context, *nonce);
    let (action, transaction_kind) = match parsing_result {
        Ok((action, transaction_kind)) => {
            // Increment nonce for all cases where the registrar contract is not needed
            // to prevent replay of those transactions. For transactions that go through
            // the registrar we still do not know if the transaction has a relayer error
            // or not, therefore we must delay incrementing the nonce.
            //
            // Note: relayers with access keys cannot use this delay to needlessly spend
            // the users tokens because only one transaction is allowed to be in-flight
            // at a time.
            if let TransactionKind::EthEmulation(EthEmulationKind::EOABaseTokenTransfer {
                address_check: Some(_),
                ..
            }) = &transaction_kind
            {
            } else {
                *nonce = nonce.saturating_add(1);
            }

            // If the action is an emulated base token or ERC-20 transfer with a non-zero fee then
            // create a promise to send the refund to the relayer. This allows any relayer
            // to safely serve base token transfers from any wallet without additional
            // on-boarding because the relayer will receive some compensation for sending
            // the transaction. Users should always verify the fee before signing a base token
            // transfer. Relayers should also verify the fee before sending to make sure the
            // user's signed transaction will refund enough to cover the relayer's gas costs.
            if let TransactionKind::EthEmulation(EthEmulationKind::EOABaseTokenTransfer {
                fee,
                ..
            })
            | TransactionKind::EthEmulation(EthEmulationKind::ERC20Transfer { fee, .. }) =
                &transaction_kind
            {
                if !fee.is_zero() && context.predecessor_account_id != context.current_account_id {
                    let refund_promise = env::promise_batch_create(&context.predecessor_account_id);
                    env::promise_batch_action_transfer(refund_promise, *fee);
                }
            }

            (action, transaction_kind)
        }
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

**File:** runtime/near-wallet-contract/implementation/wallet-contract/src/types.rs (L180-191)
```rust
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

**File:** runtime/runtime/src/lib.rs (L1249-1249)
```rust
        let deposit_refund = if result.result.is_err() { total_deposit } else { Balance::ZERO };
```
