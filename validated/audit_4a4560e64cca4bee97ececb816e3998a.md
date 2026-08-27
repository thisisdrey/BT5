### Title
`rlp_execute` relayer-fee front-running: any caller can intercept a signed wallet transaction and take the relayer incentive fee before the intended relayer - (File: `runtime/near-wallet-contract/implementation/wallet-contract/src/lib.rs`)

### Summary
The `near-wallet-contract` (the eth-implicit account "Wallet Contract") pays a caller-claimable incentive fee to whoever submits a user's pre-signed Ethereum-style transaction via `rlp_execute`. Because `rlp_execute` is a public, unauthenticated method and the fee is computed purely from the signed payload (`gas_price * gas_limit`), any third party who observes the signed transaction (e.g. in the mempool/off-chain relay channel) can resubmit it themselves and collect the fee meant to compensate the user's chosen relayer — the exact "anyone with valid input can take the incentive fee" pattern from the report.

### Finding Description
`rlp_execute` takes an eth-implicit account's RLP-encoded signed transaction and executes it with no restriction on who the NEAR caller (`predecessor_account_id`) is: [1](#0-0) 

The fee is derived solely from fields inside the user's off-chain-signed Ethereum transaction (`max_fee_per_gas * gas_limit`), independent of which account actually calls `rlp_execute`: [2](#0-1) 

When the parsed transaction is a base-token transfer or ERC-20 transfer with a non-zero fee, the contract unconditionally creates an independent transfer promise paying the fee to `context.predecessor_account_id` — i.e., whoever happened to call `rlp_execute`: [3](#0-2) 

The code comment even documents that this is intentionally open to "any relayer": "This allows any relayer to safely serve base token transfers from any wallet without additional on-boarding because the relayer will receive some compensation for sending the transaction." There is no binding between the fee payout and a specific authorized relayer identity — the only checks are on the signed transaction's `nonce`, `to`, `chain_id`, and attached gas (`validate_tx_relayer_data`): [4](#0-3) 

Because the nonce/signature validation is the only gate, once the user signs and hands the transaction to their chosen relayer (off-chain), anyone who observes that signed payload (a competing relayer, or a validator/node operator watching the mempool since it travels as a `FunctionCall` argument in a plain NEAR transaction) can front-run the intended relayer by submitting `rlp_execute` themselves with a higher-priority/faster transaction and collect the fee. The user's designated relayer loses the compensation they were promised, even though they did nothing wrong.

### Impact Explanation
The fee balance leaves the wallet account and is paid to whichever caller wins the race, not necessarily the relayer the user intended to compensate. This is a direct, permissionless diversion of funds (the incentive fee) away from its intended recipient to an opportunistic third party, mirroring the "the caller can take away the incentive fee" impact in the report. For a general-purpose relayer market this undermines the economic incentive model the fee mechanism was built to provide, and a validator/block-producer (analogous to "Bob as a miner" in the original report) is in a privileged position to always win this race against ordinary relayers.

### Likelihood Explanation
Likelihood is high: the signed RLP transaction (including its `to`, `value`, `nonce`, and fee-determining `gas_price`/`gas_limit`) must be transmitted somewhere before it reaches the wallet contract (typically via a NEAR `FunctionCall` transaction to `rlp_execute`, itself visible in the mempool once broadcast, or leaked by any intermediary the user's relayer uses). Any observer can copy the arguments and call `rlp_execute` themselves; no special privilege, access key, or state is required beyond paying their own NEAR gas.

### Recommendation
Bind the fee payout to an authorized/attested relayer rather than to `predecessor_account_id` unconditionally — for example, require the signed Ethereum transaction to designate an expected relayer/beneficiary account (part of the signed payload) and only pay the fee to that account, or restrict `rlp_execute` fee-refund behavior to relayers registered via the existing `register_relayer` access-key mechanism (`runtime/near-wallet-contract/implementation/wallet-contract/src/tests/relayer.rs`).

### Proof of Concept
1. Wallet owner (eth-implicit account) signs an Ethereum-style base-token-transfer transaction with `max_fee_per_gas * gas_limit` set to a non-trivial fee, intending to hand it to `RelayerA`.
2. Before `RelayerA` submits it, `RelayerB` (or any node that observed the pending NEAR `FunctionCall(rlp_execute, {target, tx_bytes_b64})` transaction) copies the same `target`/`tx_bytes_b64` and calls `rlp_execute` itself, paying its own NEAR gas.
3. `inner_rlp_execute` validates nonce/signature/target (all satisfied — this is a legitimately signed transaction) and unconditionally creates the fee-transfer promise to `RelayerB` (the caller), per `runtime/near-wallet-contract/implementation/wallet-contract/src/lib.rs:381-384`.
4. `RelayerA`'s subsequent submission of the same transaction fails (nonce already advanced), so `RelayerA` receives nothing despite being the intended, trusted relayer; `RelayerB` collects the fee.

### Citations

**File:** runtime/near-wallet-contract/implementation/wallet-contract/src/lib.rs (L88-114)
```rust
    #[payable]
    pub fn rlp_execute(
        &mut self,
        target: AccountId,
        tx_bytes_b64: String,
    ) -> PromiseOrValue<ExecuteResponse> {
        // To ensure user actions are executed in the desired order,
        // having multiple transactions in flight at the same time is
        // not allowed.
        if self.has_in_flight_tx {
            return PromiseOrValue::Value(ExecuteResponse {
                success: false,
                success_value: None,
                error: Some(
                    "Error: transaction already in progress, please try again later.".into(),
                ),
            });
        }
        let current_account_id = env::current_account_id();
        let predecessor_account_id = env::predecessor_account_id();
        let result = inner_rlp_execute(
            current_account_id.clone(),
            predecessor_account_id,
            target,
            tx_bytes_b64,
            &mut self.nonce,
        );
```

**File:** runtime/near-wallet-contract/implementation/wallet-contract/src/lib.rs (L367-385)
```rust
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
```

**File:** runtime/near-wallet-contract/implementation/wallet-contract/src/internal.rs (L54-64)
```rust
    // Compute the fee based on the user's Ethereum transaction.
    // This is sent as a refund to the relayer in the case of an emulated base token
    // transfer or ERC-20 transfer. The reason for this refund is that it allows a
    // user with $NEAR to use a relayer service from their wallet immediately without
    // additional on-boarding.
    let tx_fee = {
        // Limit the cost by `VALUE_MAX` since we will convert this to a $NEAR amount.
        // The call to `low_u128` is safe because `VALUE_MAX` is the largest accepted value.
        let wei_amount = tx.max_fee_per_gas.saturating_mul(tx.gas_limit).min(VALUE_MAX).low_u128();
        NearToken::from_yoctonear(wei_amount.saturating_mul(MAX_YOCTO_NEAR as u128))
    };
```

**File:** runtime/near-wallet-contract/implementation/wallet-contract/src/internal.rs (L318-368)
```rust
fn validate_tx_relayer_data<'a>(
    tx: &NormalizedEthTransaction,
    target: &'a AccountId,
    context: &ExecutionContext,
    expected_nonce: u64,
) -> Result<TargetKind<'a>, Error> {
    if tx.address.raw() != context.current_address {
        return Err(Error::Relayer(RelayerError::InvalidSender));
    }

    if tx.chain_id != Some(CHAIN_ID) {
        return Err(Error::Relayer(RelayerError::InvalidChainId));
    }

    let to = tx.to.ok_or(Error::User(UserError::EvmDeployDisallowed))?.raw();

    let target_kind = parse_target(target, context.current_address);

    // valid targets satisfy `to == target` or `to == hash(target)`
    let is_valid_target = match target_kind {
        TargetKind::CurrentAccount if to == context.current_address => {
            target == &context.current_account_id
        }
        TargetKind::EthImplicit(address) if to == address => {
            target.as_str()
                == format!("0x{}{}", hex::encode(address), context.current_account_suffix())
        }
        _ => to == account_id_to_address(target),
    };

    if !is_valid_target {
        return Err(Error::Relayer(RelayerError::InvalidTarget));
    }

    let nonce = if tx.nonce <= U64_MAX {
        tx.nonce.low_u64()
    } else {
        return Err(Error::Relayer(RelayerError::InvalidNonce));
    };
    if nonce != expected_nonce {
        return Err(Error::Relayer(RelayerError::InvalidNonce));
    }

    // Relayers must attach at least as much gas as the user requested.
    let gas_limit = if tx.gas_limit < U64_MAX { tx.gas_limit.as_u64() } else { u64::MAX };
    if env::prepaid_gas().as_gas() < gas_limit.saturating_mul(GAS_MULTIPLIER) {
        return Err(Error::Relayer(RelayerError::InsufficientGas));
    }

    Ok(target_kind)
}
```
