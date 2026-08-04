### Title
Permanent lock of ERC20-ledger funds when a registered XCM-reserve ERC20 contract is terminated via `SELFDESTRUCT` between withdraw and deposit legs - (File: `cumulus/parachains/runtimes/assets/common/src/erc20_transactor.rs`)

### Summary
`ERC20Transactor` is the XCM `TransactAsset` implementation used on Asset Hub to move ERC20 tokens (deployed on `pallet-revive`) as reserve-backed XCM assets. Its `withdraw_asset_with_surplus` and `deposit_asset_with_surplus` both perform a low-level `pallet_revive::Pallet::<T>::bare_call` invoking the ERC20 contract's `transfer(to, value)` selector, in a pattern directly analogous to `TransferHelper.safeTransfer` in the referenced report: a low-level call is issued to an arbitrary user-registered contract address, and success/failure is inferred purely from the call's return data, without ever verifying that the target still hosts live contract code. [1](#0-0) [2](#0-1) 

### Finding Description
The withdraw leg debits the user and credits `ERC20TransfersCheckingAccount` by calling `transfer(checking_address, amount)` on the ERC20 contract: [3](#0-2) 

The deposit leg later calls the *same* contract address again — `transfer(beneficiary_address, amount)` from the checking account — to release the funds to the final XCM beneficiary: [4](#0-3) 

`pallet-revive` now fully supports `SELFDESTRUCT`/`terminate`, which (post EIP‑6780 changes) removes the contract's code and, outside the same-tx-creation case, schedules the account for destruction while sending its native balance to a beneficiary: [5](#0-4) [6](#0-5) [7](#0-6) 

Separately, `pallet-revive` deliberately treats a call into an address that has no associated code as a plain balance transfer, mirroring EVM's "calling a non-existent contract succeeds" semantics called out in the report: [8](#0-7) 

Combining these facts: any ERC20 contract address can be registered as a reserve asset for XCM transfers via `ERC20Transactor`/`ERC20Matcher` (no privileged allow-list gate is evidenced in the matcher config beyond address-shape matching). The deployer of such a contract — an ordinary, unprivileged user — can legitimately let users `withdraw_asset` (moving their ERC20 balance into `ERC20TransfersCheckingAccount`'s ledger entry inside the contract's own storage) and then self-destruct the contract before the corresponding `deposit_asset` leg executes (e.g., in a multi-hop/remote-reserve XCM, or simply between message executions). Once destroyed:
- The contract's code is gone, so its internal balance-mapping logic that would move `checking_account`'s ledger entry to the beneficiary can never execute again.
- Any subsequent `bare_call` to that address for a deposit is either treated as a no-op zero-value native transfer (silently "succeeding" with empty return data) or fails to ABI-decode a `bool` from empty output, causing `XcmError::FailedToTransactAsset`.
- Either way, the tokens debited from the sender and credited to `checking_account` *inside the destroyed contract's own accounting* are permanently unreachable — there is no code left to ever call `transfer` from `checking_account` again. This is functionally identical to Alice destroying `TestToken` in the original report and locking Bob's paired ETH in the pool: the low-level call abstraction (`safeTransfer` there, `bare_call`+ABI-encoded `transfer` here) never confirms the callee still exists before trusting the transaction to represent a real value movement, so pre-committed value becomes stranded once the callee disappears.

### Impact Explanation
This qualifies as a "permanent user-fund or bridge-state lock" impact under the accepted scope: value legitimately withdrawn from a user account and credited into the `ERC20TransfersCheckingAccount`'s internal ERC20 ledger entry becomes irrecoverable once the backing contract self-destructs, because the only code path capable of moving funds out of that ledger entry (the contract's own `transfer` function) no longer exists. No malicious relayer, validator, governance action, or leaked key is required — only the ordinary contract-owner capability to call `SELFDESTRUCT`/`terminate` on a contract they deployed and voluntarily exposed via XCM asset registration.

### Likelihood Explanation
Likelihood is moderate-to-high in principle: `SELFDESTRUCT` is a first-class, now fully supported opcode/precompile action in `pallet-revive`, deployable by any contract owner, and `ERC20Transactor` performs no existence/liveness check on the target contract at either the withdraw or deposit call site — it only inspects the call's `Result`/return data after issuing the low-level call, exactly the flaw called out in the referenced report. The main external factor (as the original judge noted for the analog bug) is that this requires a token contract that is destructible and gets destroyed at an inconvenient time relative to in-flight XCM legs, which is an explicit design choice by whoever wires up the asset but not something the runtime prevents.

### Recommendation
Before trusting a `bare_call` result from `ERC20Transactor` (or any similarly structured low-level-call-based asset transactor/precompile in `pallet-revive`), verify that the target address still has associated contract code (e.g., via `AccountInfo::<T>::load_contract`/`Pallet::<T>::code`) prior to and immediately after issuing the call, and treat "call succeeded against a now-codeless address" as a hard failure rather than a silent success. Additionally, consider disallowing `SELFDESTRUCT`/`terminate` for contracts that have non-zero balances recorded in downstream ledgers still expecting further transfers, or require a settlement/quiescence check before honoring termination for contracts registered as XCM reserve assets.

### Proof of Concept
1. Deploy an ERC20 contract `T` on Asset Hub (`pallet-revive`) and have `ERC20Matcher` recognize `AccountKey20{key: T}` as an XCM-transactable asset (as in the existing `withdraw_and_deposit_erc20s` test flow).
2. User Bob executes an XCM `withdraw_asset` for `T`, which calls `ERC20Transactor::withdraw_asset_with_surplus` → `bare_call` → `T.transfer(checking_address, amount)`, succeeding and crediting `ERC20TransfersCheckingAccount`'s balance inside `T`'s own storage.
3. Before the XCM's `deposit_asset` leg executes (e.g., a delayed/queued hop, or the attacker races the mempool), the owner of `T` calls `terminate`/`SELFDESTRUCT` on `T` (supported per `self_destruct_by_syscall_works`/`self_destruct_by_precompile_works` tests), removing its code.
4. When `deposit_asset_with_surplus` later runs, `bare_call` to `T.transfer(beneficiary, amount)` either (a) is treated as a no-op zero-value transfer to a codeless account (per the "calling address without code is a balance transfer" behavior) and reports empty output, causing ABI-decode failure and `XcmError::FailedToTransactAsset`, or (b) otherwise fails, leaving the XCM `deposit_asset` unable to complete.
5. Result: Bob's tokens, already debited and recorded against `ERC20TransfersCheckingAccount` inside `T`'s now-destroyed storage, can never be retrieved — there is no surviving code path to execute a `transfer` from that ledger entry, permanently locking the funds — mirroring the `TestToken`/`safeTransfer` scenario in the referenced report.

### Citations

**File:** cumulus/parachains/runtimes/assets/common/src/erc20_transactor.rs (L163-181)
```rust
		let checking_account_eth = T::AddressMapper::to_address(&TransfersCheckingAccount::get());
		let checking_address = Address::from(Into::<[u8; 20]>::into(checking_account_eth));
		let weight_limit = WeightLimit::get();
		// To withdraw, we actually transfer to the checking account.
		// We do this using the solidity ERC20 interface.
		let data =
			IERC20::transferCall { to: checking_address, value: EU256::from(amount) }.abi_encode();
		let ContractResult { result, weight_consumed, storage_deposit, .. } =
			pallet_revive::Pallet::<T>::bare_call(
				OriginFor::<T>::signed(who.clone()),
				asset_id,
				U256::zero(),
				TransactionLimits::WeightAndDeposit {
					weight_limit,
					deposit_limit: StorageDepositLimit::get(),
				},
				data,
				&ExecConfig::new_substrate_tx(),
			);
```

**File:** cumulus/parachains/runtimes/assets/common/src/erc20_transactor.rs (L248-266)
```rust
		// We need to map the 32 byte beneficiary account to a 20 byte account.
		let eth_address = T::AddressMapper::to_address(&who);
		let address = Address::from(Into::<[u8; 20]>::into(eth_address));
		// To deposit, we actually transfer from the checking account to the beneficiary.
		// We do this using the solidity ERC20 interface.
		let data = IERC20::transferCall { to: address, value: EU256::from(amount) }.abi_encode();
		let weight_limit = WeightLimit::get();
		let ContractResult { result, weight_consumed, storage_deposit, .. } =
			pallet_revive::Pallet::<T>::bare_call(
				OriginFor::<T>::signed(TransfersCheckingAccount::get()),
				asset_contract_id,
				U256::zero(),
				TransactionLimits::WeightAndDeposit {
					weight_limit,
					deposit_limit: StorageDepositLimit::get(),
				},
				data,
				&ExecConfig::new_substrate_tx(),
			);
```

**File:** substrate/frame/revive/src/vm/evm/instructions/host.rs (L281-308)
```rust
/// Implements the SELFDESTRUCT instruction.
///
/// Halt execution and register account for later deletion.
pub fn selfdestruct<'ext, E: Ext>(interpreter: &mut Interpreter<'ext, E>) -> ControlFlow<Halt> {
	if interpreter.ext.is_read_only() {
		return ControlFlow::Break(Error::<E::T>::StateChangeDenied.into());
	}
	let [beneficiary] = interpreter.stack.popn()?;
	let charged = interpreter.ext.charge_or_halt(RuntimeCosts::Terminate { code_removed: true })?;
	let dispatch_result = interpreter.ext.terminate_if_same_tx(&beneficiary.into_address());

	match dispatch_result {
		Ok(code_removed) => {
			// halt execution on successful selfdestruct
			if matches!(code_removed, crate::CodeRemoved::No) {
				let actual_cost = RuntimeCosts::Terminate { code_removed: false };
				interpreter
					.ext
					.adjust_gas(charged, <RuntimeCosts as Token<E::T>>::weight(&actual_cost));
			}
			ControlFlow::Break(Halt::Return(Vec::default()))
		},
		Err(e) => {
			log::debug!(target: LOG_TARGET, "Selfdestruct failed: {:?}", e);
			ControlFlow::Break(Halt::Err(e))
		},
	}
}
```

**File:** prdoc/stable2512/pr_9699.prdoc (L1-16)
```text
title: Rve/revm selfdestruct2
doc:
- audience: Runtime Dev
  description: |-
    fixes https://github.com/paritytech/polkadot-sdk/issues/9621

    Behavior of `terminate` is changed in accordance with EIP-6780 (and EVM in general):
    - `terminate` only deletes the code from storage if it is called in the same transaction the contract was created.
    - `terminate` does not destroy the contract instantly. The contract is registered for destruction, which happens at the end of the transaction.
crates:
- name: pallet-revive
  bump: minor
- name: pallet-revive-fixtures
  bump: minor
- name: pallet-revive-uapi
  bump: minor
```

**File:** prdoc/stable2603/pr_10302.prdoc (L1-35)
```text
title: Fix termination
doc:
- audience: Runtime Dev
  description: |-
    This PR fixes up termination by changing the behavior to:

    - The free balance (without ed) should be send away right away to the beneficiary and not be delayed like the contract deletion.
    - The ed and storage deposit will be send away only when terminating but to the origin (delayed).
    - The scheduling of the terminate needs to be reverted if the scheduling frame reverts.
    - `SELFDESTRUCT` should be allowed inside the constructor. The issuing contract will exist as account without code for the remainder of the transaction.
    - The `terminate` pre-compile should revert if delegate called or its caller was delegate called. This is just my opinion but if we are changing semantics we can might as well add some security. We are increasing the attack surface by allowing the destruction of any contract (not only created in the current tx).


    ## Other fixes
    - Storage refunds should no longer use `BestEffort`. This is necessary to fail refunds in case some other locks (due to participation in gov for example) prevent sending them away. This is in anticipation of new pre-compiles.
    - Moved pre-compile interfaces to sol files and made them available to fixtures
    - Added some Solidity written tests to exercise error cases


    ## Further tests needed

    Those should all be written in Solidity to test both backends at the same time. No more Rust fixtures.

    @0xRVE can you take those over as I am ooo.

    - Test that checks that scheduled deletions do properly roll back if a frame fails
    - Test that value send to a contract after scheduling for deletion is send to the beneficiary (different from Eth where this balance is lost)
    - Add tests that use `SELFDESTRUCT` to `Terminate.sol`. Need https://github.com/paritytech/devops/issues/4508 but can be tested locally with newest `resolc`.
crates:
- name: pallet-revive-fixtures
  bump: patch
- name: pallet-revive
  bump: patch
- name: pallet-revive-uapi
  bump: patch
```

**File:** prdoc/stable2412/pr_5664.prdoc (L1-11)
```text
title: Calling an address without associated code is a balance transfer

doc:
  - audience: Runtime Dev
    description: |
     This makes pallet_revive behave like EVM where a balance transfer
     is just a call to a plain wallet.

crates:
  - name: pallet-revive
    bump: patch
```
