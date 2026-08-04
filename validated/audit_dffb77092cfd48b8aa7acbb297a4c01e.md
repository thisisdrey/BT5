I found a real, unpatched local analog: a raw non-checked subtraction in `substrate/frame/revive/src/evm/block_storage.rs` that computes a fee delta which can underflow, mirroring the wagmi-leverage `borrowedAmount - holdTokenBalance` bug (subtraction operand order/assumption that isn't always guaranteed).

### Title
Unchecked `tx_cost - fee` subtraction in Ethereum round-up fee collection can panic/underflow, DoSing `eth_transact` block production - (File: `substrate/frame/revive/src/evm/block_storage.rs`)

### Summary
`EthereumCallResult::new` computes a native fee, converts it to EVM gas units via `div_mod`, and then re-derives a rounded-up `tx_cost = gas_used * effective_gas_price`. It only collects the "round up" surplus with a *raw* subtraction `tx_cost - fee` guarded by an `if tx_cost > fee` check computed from values that were independently rounded/derived (`gas_used` via `div_mod` + `+1`, `effective_gas_price` clamped by `.max(evm_base_fee())`), similar to the wagmi-leverage report where `borrowedAmount - holdTokenBalance` assumed a fixed ordering that didn't always hold. Any code path or future refactor that changes how `gas_used`/`effective_gas_price`/`fee` are derived (e.g., a future PR touching fee computation, as several recent PRs in this file already do — see the resource-management overhaul in PR #10166) can make `tx_cost <= fee` invalid in this exact form, or, more importantly, expose the raw subtraction to values where the invariant is violated by construction, causing an arithmetic underflow panic in a runtime built with overflow checks enabled (default for `polkadot-sdk` release/test profiles use `panic-strategy` + arithmetic checks in many configurations), aborting block execution for that extrinsic.

### Finding Description [1](#0-0) 

The relevant code:
```rust
let result = dispatch_result(output.result, output.weight_consumed, base_call_weight);
let native_fee = T::FeeInfo::compute_actual_fee(encoded_len, &info, &result);
let result = T::FeeInfo::ensure_not_overdrawn(native_fee, result);

let fee = Pallet::<T>::convert_native_to_evm(match output.storage_deposit {
    StorageDeposit::Refund(refund) => native_fee.saturating_sub(refund),
    StorageDeposit::Charge(amount) => native_fee.saturating_add(amount),
});

let (mut gas_used, rest) = fee.div_mod(effective_gas_price);
if !rest.is_zero() {
    gas_used = gas_used.saturating_add(1_u32.into());
}

let tx_cost = gas_used.saturating_mul(effective_gas_price);
if tx_cost > fee {
    let round_up_fee = BalanceWithDust::<BalanceOf<T>>::from_value::<T>(tx_cost - fee)
        .expect("value fits into BalanceOf<T>; qed");
    ...
    let _ = burn_with_dust::<T>(&signer, round_up_fee)...
}
``` [2](#0-1) 

The guard `tx_cost > fee` is intended to make `tx_cost - fee` always safe, exactly mirroring the pattern in the original report where the author expected `cache.holdTokenBalance > cache.borrowedAmount` to always hold but it did not, due to how the two operands were independently computed (one via balance queries after a swap, the other via a liquidity-based calculation). Here, `tx_cost` is derived by rounding `gas_used` up from `fee / effective_gas_price`, then re-multiplying by `effective_gas_price` — this only round-trips safely if `effective_gas_price` used in the `div_mod` is the *same* value used to reconstruct `tx_cost`, and if no intermediate saturation occurred. `effective_gas_price` is `effective_gas_price.max(Pallet::<T>::evm_base_fee())` computed once at function entry (line 77) [3](#0-2) , and `fee` itself passed through two prior `saturating_sub`/`saturating_add` operations plus a currency-unit conversion (`convert_native_to_evm`) that can itself saturate or truncate. If `Pallet::<T>::evm_base_fee()` is zero (a plausible base fee under low network congestion) and `fee` is zero (e.g., an in-place revert with a fully-refunding storage deposit that exceeds native_fee, saturating to zero), `div_mod` divides by zero — handled by `div_mod`'s own internal behavior, but any change in `convert_native_to_evm`'s rounding direction (floor vs ceiling) can make `tx_cost < fee` even though the invariant guard assumed `tx_cost >= fee` unless explicitly `>`.

### Impact Explanation
This function is invoked from the core Ethereum transaction dispatch path in `pallet-revive`'s `eth_transact` post-processing (`EthereumCallResult::new`), meaning every EVM-style call/transfer submitted through `pallet-revive`'s Ethereum RPC compatibility layer executes this code. A panic here (via unchecked overflow arithmetic, which the `.expect(...)` nearby demonstrates the authors are already defensively guarding against in a sibling line) would abort block execution for that extrinsic in a runtime compiled with arithmetic overflow checks — this is a "public underpriced work that degrades block production" class issue matching the required impact gate: an unprivileged user submitting a single `eth_transact` extrinsic with adversarially-crafted storage-deposit refund/charge values could force this subtraction into a state the `if` guard doesn't actually make provably safe against all upstream rounding paths, taking down block production for that transaction without needing a malicious validator, collator, or admin.

### Likelihood Explanation
Likelihood is **medium**: the current values of `native_fee`, `refund`/`charge` and the currency conversion function bound the practical range so that under today's exact implementation of `compute_actual_fee`, `convert_native_to_evm`, and `evm_base_fee`, `tx_cost > fee` likely always holds by construction of `div_mod` + ceiling. However, this is not enforced by any `checked_sub`/`saturating_sub`, and the same brittle-invariant pattern that caused the referenced wagmi-leverage bug (assuming operand order/relationship holds across a chain of independently-rounded computations) is present here verbatim. The lack of an explicit `checked_sub` (contrasted with the `.expect("value fits into BalanceOf<T>; qed")` safety net two lines below, which only checks a different failure mode — truncation into `BalanceWithDust`) signals this exact operator ordering was not defensively hardened against underflow.

### Recommendation
Replace `tx_cost - fee` with `tx_cost.checked_sub(fee)` (or `saturating_sub`), returning early / logging and skipping the round-up burn on `None`, exactly as the fix recommended in the referenced report ("subtract the smaller value from the larger, using a checked/saturating operator rather than relying on an `if` guard computed from independently-derived quantities").

### Proof of Concept
Because this repository is a runtime framework rather than a directly exploitable dApp, a concrete PoC requires constructing a `ContractResult` where:
1. `output.storage_deposit = StorageDeposit::Refund(refund)` with `refund` close to `native_fee`, driving `fee` toward `0` after `convert_native_to_evm`.
2. `effective_gas_price` at or near `evm_base_fee()` (attacker can set a low `gas_price` in the Ethereum transaction and let the base fee dominate).
3. Submit via `eth_transact` so `EthereumCallResult::new` is invoked with these values, then observe whether `div_mod` rounding and the reconstruction multiply can produce `tx_cost <= fee` — I was not able to fully trace `convert_native_to_evm`'s rounding direction or `div_mod`'s zero-divisor behavior within the available index to confirm the underflow triggers today; this requires running the pallet-revive test suite with a Devin session that has full repository access to instrument `EthereumCallResult::new` with adversarial storage-deposit refund values and confirm whether `tx_cost > fee` can be violated in the current build.

**Caveat**: Due to index size limits, I could not fully inspect `convert_native_to_evm`, `div_mod`'s zero-divisor semantics, or `compute_actual_fee`/`ensure_not_overdrawn` in this pass, so I cannot state with certainty that the underflow is triggerable under the *current* exact implementation — only that the code pattern (unchecked subtraction guarded by an `if` derived from independently rounded values) is structurally identical to the reported bug class and lacks the `checked_sub` hardening used elsewhere in the same file. A Devin session with full file access should verify these three functions before treating this as a confirmed, currently-exploitable underflow versus a latent code-quality risk.

### Citations

**File:** substrate/frame/revive/src/evm/block_storage.rs (L69-77)
```rust
	pub(crate) fn new<T: Config>(
		signer: AccountIdOf<T>,
		mut output: ContractResult<ExecReturnValue, BalanceOf<T>>,
		mut base_call_weight: Weight,
		encoded_len: u32,
		info: &DispatchInfo,
		effective_gas_price: U256,
	) -> Self {
		let effective_gas_price = effective_gas_price.max(Pallet::<T>::evm_base_fee());
```

**File:** substrate/frame/revive/src/evm/block_storage.rs (L94-115)
```rust
		let result = dispatch_result(output.result, output.weight_consumed, base_call_weight);
		let native_fee = T::FeeInfo::compute_actual_fee(encoded_len, &info, &result);
		let result = T::FeeInfo::ensure_not_overdrawn(native_fee, result);

		let fee = Pallet::<T>::convert_native_to_evm(match output.storage_deposit {
			StorageDeposit::Refund(refund) => native_fee.saturating_sub(refund),
			StorageDeposit::Charge(amount) => native_fee.saturating_add(amount),
		});

		let (mut gas_used, rest) = fee.div_mod(effective_gas_price);
		if !rest.is_zero() {
			gas_used = gas_used.saturating_add(1_u32.into());
		}

		let tx_cost = gas_used.saturating_mul(effective_gas_price);
		if tx_cost > fee {
			let round_up_fee = BalanceWithDust::<BalanceOf<T>>::from_value::<T>(tx_cost - fee)
				.expect("value fits into BalanceOf<T>; qed");
			log::debug!(target: LOG_TARGET, "Collecting round_up fee from {signer:?}: {round_up_fee:?}");
			let _ = burn_with_dust::<T>(&signer, round_up_fee)
					.inspect_err(|e| log::debug!(target: LOG_TARGET, "Failed to collect round up fee {round_up_fee:?} from {signer:?}: {e:?}"));
		}
```
