### Title
Missing check that user-supplied `refund_to` is not the reserved `system` sentinel account permanently burns refunded deposits - (File: `runtime/near-vm-runner/src/logic/logic.rs`)

### Summary
The NEP-591-style `promise_set_refund_to` host function lets any unprivileged contract redirect the deposit refund of a promise it creates to an arbitrary `AccountId` supplied by the caller. Neither the host function nor the later receipt-validation step reject the reserved `system` account id, even though `system` is documented as a sentinel that can never be created or hold state and is used exclusively to mark refund receipts. Setting `refund_to` to `"system"` (a syntactically valid `AccountId`) causes the resulting deposit refund to be sent to an address that can never successfully receive funds, permanently burning the deposit instead of returning it to the rightful depositor — the exact "unvalidated destination address" bug class described in the external report, applied to NEAR's reserved-account-id analog of `address(0)`.

### Finding Description
`promise_set_refund_to` decodes the caller-supplied account id purely through generic syntax validation: [1](#0-0) 

and then stores it unchecked as the receipt's `refund_to`: [2](#0-1) 

(equivalently in the wasmtime runner at [3](#0-2) ).

When the receipt is later validated, `refund_to` again only goes through the generic `AccountId::validate` syntax check — no check excludes the reserved `system` id: [4](#0-3) 

However, `system` is explicitly documented as a non-creatable, stateless sentinel account reserved for marking refund receipts, and any failed refund targeting it is simply burnt rather than retried or redirected: [5](#0-4) [6](#0-5) 

Because `"system"` passes ordinary `AccountId` format validation (it is a valid lowercase alphanumeric string), an unprivileged contract can set `refund_to` to `"system"` for a promise funded with another account's attached deposit (e.g. a user calling a malicious/compromised contract that creates a cross-contract call on the user's behalf and calls `promise_set_refund_to(promise_idx, "system")`). If that promise subsequently fails (which the calling contract can also engineer, e.g. by calling a non-existent method as in the existing `test_refund_to` test), the deposit refund — which would normally return to the depositor — is instead routed to the unusable `system` account and burnt, per the documented "failed refund is burnt" rule.

This mirrors the reported bug class exactly: a function accepting a caller-controlled "address" (`refund_to`) fails to reject a reserved/degenerate value (`system`, the NEAR analog of `address(0)`), letting a mistake or a malicious contract silently destroy funds that should have gone to a real party.

### Impact Explanation
This allows permanent, unrecoverable loss of another account's attached deposit. Any contract can, without any special privilege, cause an unsuspecting caller's refundable deposit to be routed to the un-creatable `system` account, where — per the refund semantics of the runtime — it is burnt instead of refunded. This is a direct token-loss/permanent-freezing-of-funds impact within an unprivileged, ordinary-client-reachable code path (any contract call using `promise_set_refund_to`, a stable, non-privileged host function).

### Likelihood Explanation
The path is reachable by any deployed contract without special permissions — `promise_set_refund_to` is a standard host function available to all WASM contracts, and no privileged role is required to trigger it or to cause the underlying promise to fail. A malicious or buggy contract author needs only to know the literal string `"system"` and construct a failing promise, both of which are trivial and demonstrated already by the existing `test_refund_to` integration test pattern (which shows deliberately failing a promise and redirecting its refund).

### Recommendation
Add an explicit check — alongside the existing `AccountId::validate` calls in `promise_set_refund_to` (`runtime/near-vm-runner/src/logic/logic.rs`, `runtime/near-vm-runner/src/wasmtime_runner/logic.rs`) and/or in `validate_action_receipt` (`runtime/runtime/src/verifier.rs`) — that rejects `refund_to` values equal to the reserved `system` account id, returning an error (e.g. a new `InvalidRefundTo`/`HostError` variant) instead of silently accepting it, just as `system` is already disallowed as an ordinary account id elsewhere in the codebase.

### Proof of Concept
1. Contract `A` receives a call from user `U` with an attached deposit `D`.
2. `A` creates a promise (`account_id: X`) via `promise_batch_action_function_call`, calling a non-existent method (guaranteeing failure) and attaching `D`.
3. `A` calls `promise_set_refund_to(promise_idx, "system")`.
4. The promise executes, fails (method not found), and the runtime generates a deposit-refund receipt whose destination is `"system"` instead of `U`.
5. Per documented refund semantics, delivering funds to `system` fails/does not apply state, and the refund amount is burnt — `U`'s deposit `D` is permanently lost, confirmed by the existing test scaffold demonstrating refund redirection: [7](#0-6) , substituting the `beneficiary_id`/`refund_to` target `"near_3"` with `"system"`.

### Citations

**File:** runtime/near-vm-runner/src/logic/logic.rs (L2509-2534)
```rust
    pub fn promise_set_refund_to(
        &mut self,
        promise_idx: u64,
        account_id_len: u64,
        account_id_ptr: u64,
    ) -> Result<()> {
        self.result_state.gas_counter.pay_base(base)?;
        if self.context.is_view() {
            return Err(HostError::ProhibitedInView {
                method_name: "promise_set_refund_to".to_string(),
            }
            .into());
        }
        let refund_to = self.read_and_parse_account_id(account_id_ptr, account_id_len)?;
        let promise = self
            .promises
            .get(promise_idx as usize)
            .ok_or(HostError::InvalidPromiseIndex { promise_idx })?;

        let receipt_idx = match &promise {
            Promise::Receipt(receipt_idx) => Ok(*receipt_idx),
            Promise::NotReceipt(_) => Err(HostError::CannotSetRefundToOnJointPromise),
        }?;

        self.ext.set_refund_to(receipt_idx, refund_to);
        Ok(())
```

**File:** runtime/near-vm-runner/src/logic/logic.rs (L4262-4279)
```rust
    fn read_and_parse_account_id(&mut self, ptr: u64, len: u64) -> Result<AccountId> {
        let buf = get_memory_or_register!(self, ptr, len)?;
        self.result_state.gas_counter.pay_base(utf8_decoding_base)?;
        self.result_state.gas_counter.pay_per(utf8_decoding_byte, buf.len() as u64)?;

        let account_id_str = String::from_utf8(buf.into_owned()).map_err(|_| HostError::BadUTF8)?;

        match self.config.limit_config.account_id_validity_rules_version {
            near_primitives_core::config::AccountIdValidityRulesVersion::V0
            | near_primitives_core::config::AccountIdValidityRulesVersion::V1 =>
            {
                #[allow(deprecated)]
                Ok(AccountId::new_unvalidated(account_id_str))
            }
            near_primitives_core::config::AccountIdValidityRulesVersion::V2 => account_id_str
                .parse()
                .map_err(|_| VMLogicError::HostError(HostError::InvalidAccountId)),
        }
```

**File:** runtime/near-vm-runner/src/wasmtime_runner/logic.rs (L2556-2589)
```rust
pub fn promise_set_refund_to(
    ctx: &mut Ctx,
    memory: &mut [u8],
    promise_idx: u64,
    account_id_len: u64,
    account_id_ptr: u64,
) -> Result<()> {
    ctx.result_state.gas_counter.pay_base(base)?;
    if ctx.context.is_view() {
        return Err(HostError::ProhibitedInView {
            method_name: "promise_set_refund_to".to_string(),
        }
        .into());
    }
    let refund_to = read_and_parse_account_id(
        &mut ctx.result_state.gas_counter,
        memory,
        &ctx.registers,
        &ctx.config,
        account_id_ptr,
        account_id_len,
    )?;
    let promise = ctx
        .promises
        .get(promise_idx as usize)
        .ok_or(HostError::InvalidPromiseIndex { promise_idx })?;

    let receipt_idx = match &promise {
        Promise::Receipt(receipt_idx) => Ok(*receipt_idx),
        Promise::NotReceipt(_) => Err(HostError::CannotSetRefundToOnJointPromise),
    }?;

    ctx.ext.set_refund_to(receipt_idx, refund_to);
    Ok(())
```

**File:** runtime/runtime/src/verifier.rs (L602-606)
```rust
    if let Some(account_id) = receipt.refund_to() {
        AccountId::validate(account_id.as_ref()).map_err(|_| {
            ReceiptValidationError::InvalidRefundTo { account_id: account_id.to_string() }
        })?;
    }
```

**File:** docs/DataStructures/Account.md (L85-87)
```markdown
## System account

`system` is a special account that is only used to identify refund receipts. For refund receipts, we set the predecessor_id to be `system` to indicate that it is a refund receipt. Users cannot create or access the `system` account. In fact, this account does not exist as part of the state.
```

**File:** docs/RuntimeSpec/Refunds.md (L10-12)
```markdown
Refund receipts are identified by having `predecessor_id == "system"`. They are also special because they don't cost any gas to generate or execute. As a result, they also do not contribute to the block gas limit.

If the execution of a refund fails, the refund amount is burnt.
```

**File:** runtime/runtime/tests/test_async_calls.rs (L1204-1296)
```rust
// redirect the balance refund using `promise_refund_to`
#[test]
fn test_refund_to() {
    let group = RuntimeGroup::new(4, 4, near_test_contracts::rs_contract());

    let signer_sender = group.signers[0].clone();
    let signer_receiver = group.signers[1].clone();
    let deposit = Balance::from_yoctonear(1000);

    let data = serde_json::json!([
        {
            "batch_create": {
                "account_id": "near_2",
            },
            "id": 0
        },
        {
            "action_function_call": {
                "promise_index": 0,
                "method_name": "non_existing_function",
                "arguments": [],
                "amount": deposit,
                "gas": GAS_2,
            },
            "id": 0
        },
        {
            "set_refund_to": {
                "promise_index": 0,
                "beneficiary_id": "near_3"
            }, "id": 0
        }
    ]);

    let signed_transaction = SignedTransaction::from_actions(
        1,
        signer_sender.get_account_id(),
        signer_receiver.get_account_id(),
        &signer_sender,
        vec![Action::FunctionCall(Box::new(FunctionCallAction {
            method_name: "call_promise".to_string(),
            args: serde_json::to_vec(&data).unwrap(),
            gas: GAS_1,
            deposit,
        }))],
        CryptoHash::default(),
    );

    let handles = RuntimeGroup::start_runtimes(group.clone(), vec![signed_transaction.clone()]);
    for h in handles {
        h.join().unwrap();
    }

    println!("{:?}", group.executed_receipts);

    use near_primitives::transaction::*;
    let [r0] = &*assert_receipts!(group, signed_transaction) else {
        panic!("Incorrect number of produced receipts")
    };

    let receipts = &*assert_receipts!(group, "near_0" => r0 @ "near_1",
        ReceiptEnum::Action(ActionReceipt{actions, ..}) | ReceiptEnum::ActionV2(ActionReceiptV2{actions, ..}),
        {},
        actions,
        a0, Action::FunctionCall(function_call_action), {
            assert_eq!(function_call_action.gas, GAS_1);
            assert_eq!(function_call_action.deposit, deposit);
            assert_eq!(function_call_action.method_name, "call_promise");
        }
    );
    let [r1, refunds @ ..] = &receipts else { panic!("Incorrect number of produced receipts") };
    group.assert_gas_refunds(&refunds[..]);

    let receipts = &*assert_receipts!(group, "near_1" => r1 @ "near_2",
        ReceiptEnum::Action(ActionReceipt{actions, ..}) | ReceiptEnum::ActionV2(ActionReceiptV2{actions, ..}),
        {},
        actions,
        a0, Action::FunctionCall(function_call_action), {
            assert_eq!(function_call_action.gas, GAS_2);
            assert_eq!(function_call_action.deposit, deposit);
            assert_eq!(function_call_action.method_name, "non_existing_function");
        }
    );
    // The redirected deposit refund (to `near_3`) is emitted first; any trailing receipt is the
    // gas refund for executing this receipt.
    let [deposit_refund, refunds @ ..] = &receipts else {
        panic!("Incorrect number of produced receipts")
    };
    group.assert_gas_refunds(&refunds[..]);

    // This is the redirected refund
    assert_refund!(group, deposit_refund @ "near_3");
}
```
