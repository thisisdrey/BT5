#No Vulnerability found for this question.

The behavior described is the intended design of NEAR's `FunctionCallPermission`: the `receiver_id` field is deliberately independent of the account the key is attached to, since function-call access keys exist specifically to let an account authorize a key to call some *other* contract (e.g., a wallet key allowing calls to a specific DeFi contract). `promise_batch_action_add_key_with_function_call` reads `receiver_id` from guest memory and passes it straight into `AddKeyAction`'s `FunctionCallPermission` via `append_action_add_key_with_function_call`, attached to the receipt identified by `receipt_idx` [1](#0-0) . That `receipt_idx` is resolved by `promise_idx_to_receipt_idx_with_sir`, which looks up `promise_idx` in `self.promises` — a promise list that is local to the current `VMLogic` execution and only contains promises the current contract itself created via `promise_create`/`promise_batch_create` in this receipt's execution [2](#0-1) . The resulting `AddKeyAction` is appended only to the receipt targeting the promise's own receiver (account X, which the calling contract itself created the promise for), never to a receipt belonging to another signer or execution [3](#0-2) .

So the key is added to account X's own access key list, and the `receiver_id`/`method_names`/`allowance` fields are just metadata scoping what that key on X is allowed to call — this is X (or X's deploying contract) authorizing its own key, exactly as the NEP intends, not a forgery of trust or a cross-account authorization escalation. There is no code path shown where an attacker can cause a key to be added to an account they don't control, since `self.promises` is scoped per execution and receipt indices cannot be aliased across unrelated receipts/signers. This is self-harm/self-configuration at worst, not an exploitable vulnerability against a third party.

### Citations

**File:** runtime/near-vm-runner/src/logic/logic.rs (L2537-2556)
```rust
    /// Helper function to return the receipt index corresponding to the given promise index.
    /// It also pulls account ID for the given receipt and compares it with the current account ID
    /// to return whether the receipt's account ID is the same.
    fn promise_idx_to_receipt_idx_with_sir(
        &self,
        promise_idx: u64,
    ) -> Result<(ReceiptIndex, bool)> {
        let promise = self
            .promises
            .get(promise_idx as usize)
            .ok_or(HostError::InvalidPromiseIndex { promise_idx })?;
        let receipt_idx = match &promise {
            Promise::Receipt(receipt_idx) => Ok(*receipt_idx),
            Promise::NotReceipt(_) => Err(HostError::CannotAppendActionToJointPromise),
        }?;

        let account_id = self.ext.get_receipt_receiver(receipt_idx);
        let sir = account_id == &self.context.current_account_id;
        Ok((receipt_idx, sir))
    }
```

**File:** runtime/near-vm-runner/src/logic/logic.rs (L3492-3538)
```rust
    pub fn promise_batch_action_add_key_with_function_call(
        &mut self,
        promise_idx: u64,
        public_key_len: u64,
        public_key_ptr: u64,
        nonce: u64,
        allowance_ptr: u64,
        receiver_id_len: u64,
        receiver_id_ptr: u64,
        method_names_len: u64,
        method_names_ptr: u64,
    ) -> Result<()> {
        self.result_state.gas_counter.pay_base(base)?;
        if self.context.is_view() {
            return Err(HostError::ProhibitedInView {
                method_name: "promise_batch_action_add_key_with_function_call".to_string(),
            }
            .into());
        }
        let public_key = self.get_public_key(
            public_key_ptr,
            public_key_len,
            self.ext.post_quantum_keys_enabled(),
        )?;
        let allowance = Balance::from_yoctonear(
            self.memory.get_u128(&mut self.result_state.gas_counter, allowance_ptr)?,
        );
        let allowance = if allowance > Balance::ZERO { Some(allowance) } else { None };
        let receiver_id = self.read_and_parse_account_id(receiver_id_ptr, receiver_id_len)?;
        let raw_method_names = get_memory_or_register!(self, method_names_ptr, method_names_len)?;
        let method_names = split_method_names(&raw_method_names)?;

        let (receipt_idx, sir) = self.promise_idx_to_receipt_idx_with_sir(promise_idx)?;

        let num_bytes = null_terminated_method_names_len(&method_names);
        self.pay_action_base(ActionCosts::add_function_call_key_base, sir)?;
        self.pay_action_per_byte(ActionCosts::add_function_call_key_byte, num_bytes, sir)?;

        self.ext.append_action_add_key_with_function_call(
            receipt_idx,
            public_key.decode()?,
            nonce,
            allowance,
            receiver_id,
            method_names,
        )?;
        Ok(())
```

**File:** runtime/runtime/src/receipt_manager.rs (L576-606)
```rust
    pub(super) fn append_action_add_key_with_function_call(
        &mut self,
        receipt_index: ReceiptIndex,
        public_key: PublicKey,
        nonce: Nonce,
        allowance: Option<Balance>,
        receiver_id: AccountId,
        method_names: Vec<Vec<u8>>,
    ) -> Result<(), VMLogicError> {
        self.append_action(
            receipt_index,
            Action::AddKey(Box::new(AddKeyAction {
                public_key,
                access_key: AccessKey {
                    nonce,
                    permission: AccessKeyPermission::FunctionCall(FunctionCallPermission {
                        allowance,
                        receiver_id: receiver_id.into(),
                        method_names: method_names
                            .into_iter()
                            .map(|method_name| {
                                String::from_utf8(method_name)
                                    .map_err(|_| HostError::InvalidMethodName)
                            })
                            .collect::<std::result::Result<Vec<_>, _>>()?,
                    }),
                },
            })),
        );
        Ok(())
    }
```
