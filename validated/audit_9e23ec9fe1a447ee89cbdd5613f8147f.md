### Title
Double payment to relayer via unconditional fee transfer plus full caller-deposit refund on failed emulation call - (File: `runtime/near-wallet-contract/implementation/wallet-contract/src/lib.rs`)

### Summary
The eth-implicit Wallet Contract's `rlp_execute` flow can pay the same relayer/caller twice for a single deposited amount: once via an unconditional "fee" transfer sent immediately, and a second time via a full refund of the original attached deposit when the downstream cross-contract call subsequently fails. Neither payment path accounts for the other, so the wallet's own balance (not just the caller's deposit) is drained by the duplicated amount — structurally the same root cause as the reported bug: two independent value-moving code paths keyed off the same "originalCaller"/deposit context, where the second path fails to subtract what the first already paid out.

### Finding Description
`inner_rlp_execute` (`runtime/near-wallet-contract/implementation/wallet-contract/src/lib.rs:330-473`) builds an `ExecutionContext` from `env::attached_deposit()` and computes `caller_deposit = CallerDeposit::new(&context)`, which stores the **full** `context.attached_deposit` for a predecessor account whenever `predecessor_account_id != current_account_id`: [1](#0-0) 

Separately, for `EOABaseTokenTransfer`/`ERC20Transfer` actions carrying a non-zero embedded `fee` (parsed from the Ethereum transaction's `max_fee_per_gas * gas_limit`, independent of the NEAR-level attached deposit), the contract unconditionally sends `fee` yoctoNEAR from its own balance to the same `predecessor_account_id`, before the outcome of the main action is known: [2](#0-1) 

The main action is then dispatched via a promise chain (`address_check_callback` / `nep_141_storage_balance_callback`) that always terminates in `rlp_execute_callback(caller_deposit)`. If the final cross-contract promise fails, the callback refunds the **entire original `caller_deposit.yocto_near`** (i.e., the whole `attached_deposit`, not reduced by the `fee` already paid out) back to the same account: [3](#0-2) 

Because both the `fee` payment and the `caller_deposit` value are derived independently — `fee` from the Ethereum tx fields and `caller_deposit` from the raw NEAR `attached_deposit` — and both are paid to the same `predecessor_account_id` under the same condition (`predecessor_account_id != current_account_id`), a relayer that attaches a NEAR deposit while relaying an ERC-20/base-token-transfer Ethereum transaction with a non-zero `fee` receives `fee` unconditionally *and* the full `attached_deposit` back again if the downstream call fails. This is analogous to the reported zkSync bug where the bridge contract already moved funds once (`transferFrom`) and then a second, independently-triggered code path (`bridgeBurn` keyed on the same original caller) moved the same value again without accounting for the first transfer.

### Impact Explanation
Every yoctoNEAR of `fee` paid out this way is not actually backed by a corresponding debit elsewhere — the caller's own deposit is refunded to them in full regardless of the fee already sent, so the wallet's non-deposit balance (which could include the wallet owner's own funds, since the eth-implicit account has a single fungible balance) is reduced without any offsetting credit. This constitutes an unaccounted-for outbound value transfer (fund loss) triggerable by an unprivileged relayer against a wallet contract account, reachable purely through the standard `rlp_execute` entry point used by any Ethereum-compatible relayer.

### Likelihood Explanation
The condition requires (1) a non-owner predecessor calling `rlp_execute` with attached NEAR deposit, (2) an Ethereum tx decoded as `EOABaseTokenTransfer`/`ERC20Transfer` with non-zero `max_fee_per_gas * gas_limit`, and (3) the downstream cross-contract call (transfer/ERC-20 transfer) ultimately failing (e.g. insufficient balance on the emulated token, or `ft_transfer` refusing due to receiver not being registered / balance checks). All of these are attacker-controllable by a malicious or self-dealing relayer who crafts a transaction destined to fail after the fee is already paid — so likelihood is plausible in production usage of the ETH-implicit wallet feature, though it requires deliberately combining "attach a deposit to `rlp_execute`" with "encode a non-zero fee" and "cause the transfer to fail," a somewhat specific but fully attacker-controlled combination.

### Recommendation
When constructing the `caller_deposit` refund amount, subtract any `fee` amount that was already unconditionally transferred to the same `predecessor_account_id` before dispatching the main promise, so the total refunded-plus-fee never exceeds the originally attached deposit. Alternatively, defer the `fee` payment until `rlp_execute_callback` succeeds, and compute the failure-path refund independently from the success-path fee payment so the two can never both fire for the same value.

### Proof of Concept
1. A relayer calls `wallet_contract.rlp_execute(target, tx_bytes_b64)` and attaches N NEAR as `attached_deposit` (as demonstrated feasible in `test_caller_refunds`, `runtime/near-wallet-contract/implementation/wallet-contract/src/tests/sanity.rs:170-229`, which shows external accounts can attach deposit and be refunded on failure).
2. The RLP-encoded Ethereum transaction encodes an `ERC20Transfer` (or `EOABaseTokenTransfer`) action targeting a token/receiver, with `max_fee_per_gas * gas_limit` set to a non-zero `fee` (see fee computation at `runtime/near-wallet-contract/implementation/wallet-contract/src/internal.rs:54-64`).
3. `inner_rlp_execute` immediately fires a transfer of `fee` to the relayer (`lib.rs:381-384`) and records `caller_deposit` = full attached N NEAR (`types.rs:180-191`).
4. The downstream `ft_transfer` (or transfer) promise fails (e.g., insufficient token balance, or the receiver storage-deposit/transfer chain reverts).
5. `rlp_execute_callback` sees `PromiseResult::Failed` and refunds the full N NEAR `caller_deposit` back to the relayer (`lib.rs:296-305`).
6. Net result: relayer receives `fee + N` NEAR total while having only deposited `N`, with the extra `fee` drained from the wallet contract's own balance.

### Citations

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

**File:** runtime/near-wallet-contract/implementation/wallet-contract/src/lib.rs (L276-317)
```rust
    pub fn rlp_execute_callback(
        &mut self,
        caller_deposit: Option<CallerDeposit>,
    ) -> ExecuteResponse {
        self.has_in_flight_tx = false;
        let n = env::promise_results_count();

        if n == 0 {
            // `rlp_execute_callback` is called directly in the case of an emulated self-transfer.
            return ExecuteResponse { success: true, success_value: None, error: None };
        } else if n > 1 {
            return ExecuteResponse {
                success: false,
                success_value: None,
                error: Some(format!(
                    "Invariant violation: this callback comes after a single promise. n={n}"
                )),
            };
        }

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
