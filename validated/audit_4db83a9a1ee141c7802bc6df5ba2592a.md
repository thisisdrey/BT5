### Title
Front-runnable `rlp_execute()` nonce consumption can trigger unauthorized ban (revocation) of a legitimate relayer's access key on ETH-implicit Wallet Contracts - (File: `runtime/near-wallet-contract/implementation/wallet-contract/src/lib.rs`)

### Summary
The Wallet Contract's `rlp_execute` entry point is a public, unauthenticated function (any NEAR account may call it, not just the intended relayer) that accepts an RLP-encoded, Secp256k1-signed Ethereum transaction and advances the wallet's internal replay-protection `nonce` once the embedded transaction is judged well-formed enough to reach the "increment" branches. Because the signed payload (`tx_bytes_b64`) is visible in the public mempool once a legitimate relayer submits it, an attacker can copy that exact payload into their own transaction and get it included first, consuming the nonce before the intended relayer's transaction lands.

### Finding Description
`rlp_execute` has no access-control restricting the caller: it is a plain `#[payable] pub fn` and dispatches to `inner_rlp_execute`, which increments `self.nonce` on the success path and even on some `Error::User` paths before the caller's own transaction is known to be the "chosen" relay. [1](#0-0) [2](#0-1) 

Because `rlp_execute(target, tx_bytes_b64)` takes the fully-signed Ethereum transaction bytes as a plain argument, and NEAR transaction contents are visible to anyone observing the mempool/gossip before inclusion, any observer can extract `tx_bytes_b64` from a pending relayer transaction and resubmit the identical call from their own account, racing to be included first (classic front-running, mirroring how the reported bug let an attacker "listen for `Pool.buyNFT()` transactions, get the signature from the oracle, and call `verifyLoanValidity()`").

The critical amplification here (beyond a simple relay race) is the ban logic: if the *intended* relayer signs their `rlp_execute` call using a dedicated access key added directly to the wallet account (`env::signer_account_id() == current_account_id`, the documented onboarding flow for "a relayer's public key to an ETH-implicit account"), and their transaction then hits a stale/already-consumed nonce because an attacker's copy landed first, `inner_rlp_execute`/`validate_tx_relayer_data` returns `Error::Relayer(RelayerError::InvalidNonce)`. [3](#0-2) 

`rlp_execute` treats any `Error::Relayer(_)` from a self-signing relayer as proof of a "faulty relayer" and immediately fires `create_ban_relayer_promise`, which (per the module's own documentation) revokes the relayer's access key: [4](#0-3) [5](#0-4) 

The `RelayerError` module comment states explicitly: *"Errors which should never happen if the relayer is honest. If these errors happen then we should ban the relayer (revoke their access key)."* This assumes `InvalidNonce` can only occur due to relayer misbehavior, but it can equally be induced by a third party front-running the relayer's identical, honestly-crafted call. [6](#0-5) 

### Impact Explanation
This is an authorization-escalation/permanent-loss vector: an unauthenticated third party can force the on-chain revocation of another account's authorized access key (the relayer's dedicated `FunctionCall` key on the wallet account) purely by observing and replaying public transaction data — without ever needing that key or the wallet owner's Secp256k1 private key. Once the key is deleted, the relayer permanently loses its ability to act on behalf of that ETH-implicit wallet until re-onboarded, which is a form of griefing/DoS with an authorization-escalation flavor matching the reported vulnerability class (front-running increments a replay-protection nonce/state, causing the legitimate submitter's subsequent transaction to fail/be penalized).

### Likelihood Explanation
Exploitation requires:
1. Monitoring the mempool/gossip layer for `rlp_execute` calls targeting a given wallet, which is trivially observable to any network participant.
2. Copying the `tx_bytes_b64` argument and resubmitting it in a competing transaction with sufficient priority (e.g., higher gas price) to land first.
3. The victim relayer using a dedicated access key added to the wallet account (a documented, encouraged onboarding pattern) rather than paying via their own named account.

Given standard NEAR mempool visibility and the fact that the whole design point of `rlp_execute` is that "any relayer to safely serve base token transfers from any wallet without additional onboarding," attacker-observable front-running is straightforward; the ban-triggering condition depends on relayers using the self-key-based onboarding flow, which narrows but does not eliminate the practical likelihood.

### Recommendation
Do not treat a bare `InvalidNonce` error as conclusive proof of relayer misbehavior, since it can be induced by an unrelated party front-running an identical, honestly-submitted `tx_bytes_b64`. Before revoking (banning) a relayer's key, verify that the nonce mismatch could not be explained by an already-successful execution of the same signed Ethereum transaction (e.g., by checking whether the corresponding action was already applied, or by only banning on more specific tamper indicators such as `InvalidSender`/`InvalidTarget`/`InvalidBase64`/`TxParsing`, which cannot be triggered by a mere front-run of valid data).

### Proof of Concept
1. Wallet owner signs an Ethereum transaction (nonce `N`) and hands it (`tx_bytes_b64`) to their relayer.
2. Relayer, using a dedicated access key added to the wallet account, submits `SignedTransaction::from_actions(..., signer=relayer_key_on_wallet, receiver=wallet, [FunctionCall(rlp_execute, {target, tx_bytes_b64})])` to the network.
3. Before this transaction is included, an attacker observes it in the mempool, copies the identical `target`/`tx_bytes_b64`, and submits their own `SignedTransaction` calling `rlp_execute` from an unrelated account with a higher gas price, getting included first — this call passes `validate_tx_relayer_data` (nonce `N` matches) and increments `self.nonce` to `N+1`.
4. The original relayer's transaction is now included; `validate_tx_relayer_data` computes `expected_nonce = N+1` but the transaction still encodes nonce `N`, returning `RelayerError::InvalidNonce`.
5. Because the relayer signed with the wallet's own access key (`env::signer_account_id() == current_account_id`), `rlp_execute` calls `create_ban_relayer_promise`, revoking the relayer's access key on the wallet — a permanent, unauthorized-by-the-relayer loss of their authorization, triggered entirely by an unprivileged third party.

Note: I was not able to retrieve the full body of `create_ban_relayer_promise` (only its declaration site was located before the tool budget was exhausted), so the exact mechanics of the key revocation (e.g., whether it deletes a specific public key or all keys) are inferred from the surrounding documentation/comments (`RelayerError` doc, `ban_relayer` function) rather than directly verified in the promise-construction code.

### Citations

**File:** runtime/near-wallet-contract/implementation/wallet-contract/src/lib.rs (L88-128)
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
    }
```

**File:** runtime/near-wallet-contract/implementation/wallet-contract/src/lib.rs (L330-365)
```rust
fn inner_rlp_execute(
    current_account_id: AccountId,
    predecessor_account_id: AccountId,
    target: AccountId,
    tx_bytes_b64: String,
    nonce: &mut u64,
) -> Result<Promise, Error> {
    if *nonce == u64::MAX {
        return Err(Error::AccountNonceExhausted);
    }
    let context = ExecutionContext::new(
        current_account_id.clone(),
        predecessor_account_id,
        env::attached_deposit(),
    )?;
    let caller_deposit = CallerDeposit::new(&context);

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
```

**File:** runtime/near-wallet-contract/implementation/wallet-contract/src/internal.rs (L352-359)
```rust
    let nonce = if tx.nonce <= U64_MAX {
        tx.nonce.low_u64()
    } else {
        return Err(Error::Relayer(RelayerError::InvalidNonce));
    };
    if nonce != expected_nonce {
        return Err(Error::Relayer(RelayerError::InvalidNonce));
    }
```

**File:** runtime/near-wallet-contract/implementation/wallet-contract/src/error.rs (L21-43)
```rust
/// Errors which should never happen if the relayer is honest.
/// If these errors happen then we should ban the relayer (revoke their access key).
/// An external caller (as opposed to a relayer with a Function Call access key) may
/// also trigger these errors by passing bad arguments, but this is not an issue
/// (there is no ban list for external callers) because they are paying the gas fees
/// for their own mistakes.
#[derive(Debug, PartialEq, Eq, Clone)]
pub enum RelayerError {
    /// Relayers should always check the nonce before sending
    InvalidNonce,
    /// Relayers should always encode the transaction correctly
    InvalidBase64,
    /// Relayers should always send valid transactions
    TxParsing(aurora_engine_transactions::Error),
    /// Relayers should always send correctly signed transactions
    InvalidSender,
    /// Relayers should always give the correct target account
    InvalidTarget,
    /// Relayers should always check the transaction is signed with the correct chain id.
    InvalidChainId,
    /// Relayers should always attach at least as much gas as the user requested.
    InsufficientGas,
}
```
