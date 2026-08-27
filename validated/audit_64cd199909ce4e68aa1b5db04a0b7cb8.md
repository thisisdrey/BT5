Confirms `CallerDeposit` only tracks the `attached_deposit` (the value being sent), not the `fee`. The fee is sent via an independent, unconditional promise batch at parse time, decoupled from whether the underlying action ultimately succeeds.

### Title
Relayer fee paid unconditionally before underlying transfer/ERC20-transfer executes, causing permanent user-fund loss on failure - (File: runtime/near-wallet-contract/implementation/wallet-contract/src/lib.rs)

### Summary
The eth-implicit Wallet Contract emulates Ethereum base-token and ERC-20 transfers by decoding an RLP transaction and scheduling NEAR promises. When the emulated transaction carries a non-zero relayer `fee`, `inner_rlp_execute` immediately creates and dispatches an independent `promise_batch_transfer` to pay the relayer, *before* the actual requested action (the base-token `Transfer` or the ERC-20 `ft_transfer`) has even been scheduled or resolved.

### Finding Description
In `inner_rlp_execute` [1](#0-0) , whenever the parsed transaction is `EOABaseTokenTransfer` or `ERC20Transfer` with a non-zero `fee`, the contract fires a standalone `env::promise_batch_create` / `env::promise_batch_action_transfer` to the relayer immediately, completely decoupled from the promise chain that will actually attempt the requested transfer.

The requested action is only executed afterward, and can fail independently of the fee payment in several ways:
- For `ERC20Transfer`, the fee is paid before even calling `storage_balance_of` on the token contract; if that call fails, `nep_141_storage_balance_callback` returns an error and the `ft_transfer` (or `storage_deposit`+`ft_transfer`) is never attempted at all [2](#0-1) .
- For an `EOABaseTokenTransfer` with an address check, if the registrar `lookup` call fails, `address_check_callback` returns an error and again the underlying transfer is never scheduled [3](#0-2) .
- Even when the underlying transfer promise is scheduled and later fails on-chain, `rlp_execute_callback` only refunds the caller's `caller_deposit` (the transfer amount attached by an external caller), not the fee, which was already spent [4](#0-3) .

`CallerDeposit` is explicitly scoped only to `context.attached_deposit` (the transfer value), never to the `fee` [5](#0-4) , so there is no accounting path that claws back or escrows the fee pending success of the action it is meant to compensate.

This mirrors the External Report's root cause: an operation (paying out value) is performed based on an *assumption* that a dependent action will succeed, without any invariant tying the payout to actual completion — analogous to `topupMarketBalance` crediting `marketBalance` without confirming the ERC20 `transferFrom` actually moved funds.

### Impact Explanation
Any ordinary user who signs an Ethereum-emulated base-token transfer or ERC-20 `transfer` through their eth-implicit wallet, with a non-zero relayer `fee`, permanently loses that fee whenever the underlying transfer fails for reasons entirely outside the fee-payment logic — a token contract's `storage_balance_of` call running out of gas, the address registrar being congested, the receiver's `ft_transfer` failing (e.g., insufficient token balance or paused contract), or the receiver account not existing. The fee is not refunded even though the actioned value transfer that it was meant to pay for never happened, resulting in real, un-recoverable loss of NEAR tokens for a service that was never rendered.

### Likelihood Explanation
This is reachable by any unprivileged signer using the eth-implicit wallet's standard `rlp_execute` flow with a relayer fee — a normal use case documented in the code itself (relayers are expected to charge fees for base-token and ERC-20 transfers). No special privileges, malicious node behavior, or governance action is required; a single failed downstream cross-contract call (which can occur under ordinary conditions such as gas mis-estimation or the destination contract being paused) triggers the loss.

### Recommendation
Either (a) escrow the fee and only release it to the relayer inside `rlp_execute_callback` upon `PromiseResult::Successful`, refunding it back to the caller alongside `caller_deposit` on `PromiseResult::Failed`, or (b) chain the fee-transfer promise after the main action promise so it only fires once the underlying transfer/ERC20-transfer has been confirmed successful, mirroring how `caller_deposit` is already tracked and refunded on failure.

### Proof of Concept
1. An eth-implicit wallet holder signs an RLP-encoded ERC-20 `transfer(to, value)` transaction targeting a NEP-141 token contract, with a relayer fee encoded via the wallet contract's fee mechanism (non-zero `fee` in `EthEmulationKind::ERC20Transfer`).
2. A relayer submits it via `rlp_execute`. `inner_rlp_execute` immediately dispatches `promise_batch_action_transfer` sending `fee` to the relayer (`context.predecessor_account_id`) [6](#0-5) .
3. The subsequent `storage_balance_of` cross-contract call to the token contract fails (e.g., insufficient attached gas, or the token contract is temporarily unavailable/paused).
4. `nep_141_storage_balance_callback` sees `PromiseResult::Failed` and returns `ExecuteResponse{success:false,...}` without ever scheduling `ft_transfer` [7](#0-6) .
5. The user's tokens were never transferred (the ERC-20 `transfer` never executed), yet the relayer fee sent in step 2 is not reversed — the user has permanently lost the fee for a transaction that accomplished nothing.

### Citations

**File:** runtime/near-wallet-contract/implementation/wallet-contract/src/lib.rs (L141-148)
```rust
        let maybe_account_id: Option<AccountId> = match env::promise_result(0) {
            PromiseResult::Failed => {
                return PromiseOrValue::Value(ExecuteResponse {
                    success: false,
                    success_value: None,
                    error: Some("Call to Address Registrar contract failed".into()),
                });
            }
```

**File:** runtime/near-wallet-contract/implementation/wallet-contract/src/lib.rs (L203-221)
```rust
        let maybe_storage_balance: Option<StorageBalance> = match env::promise_result(0) {
            PromiseResult::Failed => {
                return PromiseOrValue::Value(ExecuteResponse {
                    success: false,
                    success_value: None,
                    error: Some(format!("Call to NEP-141 {token_id}::storage_balance_of failed")),
                });
            }
            PromiseResult::Successful(value) => match serde_json::from_slice(&value) {
                Ok(x) => x,
                Err(_) => {
                    return PromiseOrValue::Value(ExecuteResponse {
                        success: false,
                        success_value: None,
                        error: Some("Unexpected response from NEP-141 storage_balance_of".into()),
                    });
                }
            },
        };
```

**File:** runtime/near-wallet-contract/implementation/wallet-contract/src/lib.rs (L296-316)
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
```

**File:** runtime/near-wallet-contract/implementation/wallet-contract/src/lib.rs (L374-385)
```rust
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
