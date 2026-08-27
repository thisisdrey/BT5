The described scenario is not a vulnerability — it is deterministic, documented protocol behavior, not a bug.

**Root cause investigation:** When a `DelegateAction` is processed, `apply_delegate_action` in `runtime/runtime/src/actions.rs` builds the new inner receipt by explicitly copying `signer_id`/`signer_public_key` from the **outer** `action_receipt` (i.e., the relayer's transaction context), not from the delegate action's `sender_id`/`public_key`: [1](#0-0) 

This is a single, unconditional code path — there is no protocol-version-gated branching or alternate VMContext-population path for delegate-action receipts, so the result is fully deterministic and identical on every node. `execute_function_call` in `runtime/runtime/src/function_call.rs` then populates `VMContext::signer_account_pk` straight from `action_receipt.signer_public_key()`: [2](#0-1) 

This behavior is explicitly specified in the NEP-366 meta-transactions spec, documented in the repo itself: "All actions inside `delegate_action.actions` are submitted with the `delegate_action.sender_id` as the predecessor, `delegate_action.receiver_id` as the receiver, and the relayer (predecessor of `DelegateAction`) as the signer": [3](#0-2) 

Runtime unit tests (`test_delegate_action` in `runtime/runtime/src/actions.rs`) explicitly assert that the generated inner receipt's `signer_id`/`signer_public_key` equal the outer `action_receipt`'s (relayer's) values, confirming this is intended, tested behavior rather than an inconsistency: [4](#0-3) 

**Why this doesn't constitute the claimed vulnerability:**
- There is no "which code path populates `ctx.context.signer_account_pk`" ambiguity — there's exactly one path, unconditional on protocol version, so all honest nodes compute an identical, deterministic result. No state-root divergence or cross-node inconsistency is possible.
- The relayer being reported as `signer_account_pk` for delegated inner actions is the specified, intended NEP-366 semantic, not an accidental bug — a contract author is expected to know that under meta-transactions, `signer_account_id`/`signer_account_pk` reflect the relayer, while `predecessor_account_id` reflects the original sender (`delegate_action.sender_id`). Contracts wanting to authorize based on the original delegate signer should use `predecessor_account_id`, not `signer_account_pk`.
- No new authorization escalation is introduced by the runtime: a contract that misuses `signer_account_pk` (instead of `predecessor_account_id`) for allowlist checks has a contract-level design flaw, not a nearcore protocol/runtime vulnerability, and this is outside the scope of a nearcore code audit.

#No vulnerability found for this question.

### Citations

**File:** runtime/runtime/src/actions.rs (L483-497)
```rust
    // Generate a new receipt from DelegateAction.
    let new_receipt = Receipt::V0(ReceiptV0 {
        predecessor_id: sender_id.clone(),
        receiver_id: delegate_action.receiver_id().clone(),
        receipt_id: CryptoHash::default(),

        receipt: ReceiptEnum::Action(ActionReceipt {
            signer_id: action_receipt.signer_id().clone(),
            signer_public_key: action_receipt.signer_public_key().clone(),
            gas_price: action_receipt.gas_price(),
            output_data_receivers: vec![],
            input_data_ids: vec![],
            actions: delegate_action.get_actions(),
        }),
    });
```

**File:** runtime/runtime/src/actions.rs (L1332-1347)
```rust
        assert_eq!(
            result.new_receipts,
            vec![Receipt::V0(ReceiptV0 {
                predecessor_id: sender_id.clone(),
                receiver_id: signed_delegate_action.delegate_action.receiver_id.clone(),
                receipt_id: CryptoHash::default(),
                receipt: ReceiptEnum::Action(ActionReceipt {
                    signer_id: action_receipt.signer_id.clone(),
                    signer_public_key: action_receipt.signer_public_key.clone(),
                    gas_price: action_receipt.gas_price,
                    output_data_receivers: Vec::new(),
                    input_data_ids: Vec::new(),
                    actions: signed_delegate_action.delegate_action.get_actions(),
                }),
            })]
        );
```

**File:** runtime/runtime/src/function_call.rs (L257-261)
```rust
    let context = VMContext {
        current_account_id: runtime_ext.account_id().clone(),
        signer_account_id: action_receipt.signer_id().clone(),
        signer_account_pk: borsh::to_vec(&action_receipt.signer_public_key())
            .expect("Failed to serialize"),
```

**File:** docs/RuntimeSpec/Actions.md (L363-367)
```markdown
### Outcomes

- All actions inside `delegate_action.actions` are submitted with the `delegate_action.sender_id` as the predecessor, `delegate_action.receiver_id` as the receiver, and the relayer (predecessor of `DelegateAction`) as the signer.
- All gas and balance costs for submitting `delegate_action.actions` are subtracted from the relayer.

```
