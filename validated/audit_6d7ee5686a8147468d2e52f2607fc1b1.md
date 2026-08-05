Audit Report

## Title
Reentrancy-protection bypass for value-transferring EVM `CALL`/`.transfer()`/`.send()` in `pallet-revive` — ([File: substrate/frame/revive/src/vm/evm/instructions/contract.rs])

## Summary
`run_call` selects the `ReentrancyProtection` mode using `(value.is_zero(), gas_limit == CALL_STIPEND)`, and the very first match arm `(false, _) => (true, ReentrancyProtection::AllowReentry)` unconditionally catches every nonzero-value call before the stipend check is even evaluated. Because solc emits `gas_limit = 0` (not `2300`) for `.transfer(amount)`/`.send(amount)` with `amount > 0` — confirmed in `prdoc/stable2603/pr_11227.prdoc` — these two most common Solidity ETH-payout idioms fall into `AllowReentry` and never receive the `AllowNext` guard, even though they are granted the identical stipend weight budget as the `AllowNext` case.

## Finding Description
The dispatch logic in `run_call` is: [1](#0-0) 

The match evaluates `value.is_zero()` first; `(false, _)` matches unconditionally whenever `value != 0`, regardless of `gas_limit`. Per the compiler-behavior table documented in the repo's own PR notes, `.transfer(amount)` and `.send(amount)` for `amount > 0` are compiled with `gas_limit = 0` (the EVM implicitly adds the 2300 stipend), not `gas_limit = 2300`: [2](#0-1) 

Only the degenerate `transfer(0)`/`send(0)` case (`value == 0`, `gas_limit == 2300`) reaches the `AllowNext` arm.

Critically, inspecting the resource-metering logic in `substrate/frame/revive/src/metering/math.rs` shows that `add_stipend` (which is `true` in *both* the `(false, _)` and `(_, true)` arms) is what actually grants the extra `determine_call_stipend::<T>()` weight budget to the nested call frame, independent of which `ReentrancyProtection` variant is chosen: [3](#0-2)  This means the real `.transfer(amount>0)` call receives exactly the same stipend-weight budget as the `transfer(0)` case that the `AllowNext` protection was designed to compensate for, but does not receive the `AllowNext` guard itself.

The `ReentrancyProtection::AllowNext` documentation explicitly states the rationale that motivates this guard: [4](#0-3)  Since the same stipend-weight conversion applies to both paths, the risk described in this comment applies equally to the real `.transfer(amount)`/`.send(amount)` case, yet that path is left on `AllowReentry`.

## Impact Explanation
This is a runtime bug in `pallet-revive`'s EVM-compatibility layer that can compromise the intended reentrancy-safety behavior for the most common Solidity ETH-payout pattern. If the converted stipend weight is sufficient for a callee to perform another external call (the exact risk called out in the pallet's own `AllowNext` doc comment), an attacker-controlled contract can reenter a victim contract during a `.transfer()`/`.send()` payout, enabling duplicate withdrawal / unbacked balance credit inside deployed EVM contracts on a `pallet-revive`-based chain — a reachable "runtime bug that compromises intended behavior" and potential duplicate-settlement/theft scenario.

## Likelihood Explanation
The path is reachable by any unprivileged account calling any deployed contract that uses `.transfer()`/`.send()` for ETH payouts — one of the most widely used Solidity idioms — via a normal `eth_transact`/call extrinsic, requiring no privileged access, governance, or compromised infrastructure. The exploit further depends on the stipend-converted weight budget actually being sufficient for a reentrant call at revive's metering scale, which is an empirical condition not verified in this review but is exactly the condition the pallet's own comment says "does not automatically hold" and must be defended against.

## Recommendation
Reorder/restructure the match so that the "stipend-style transfer" determination (any of `gas_limit == 0` or `gas_limit == CALL_STIPEND` combined with `value != 0`, or more precisely mirroring solc's actual table) uniformly selects `ReentrancyProtection::AllowNext`, independent of whether `value` happens to be zero. The guard decision should be driven by whether `add_stipend` is granted, since that is what determines whether the callee gets extra weight budget that could enable a reentrant call — not by an unrelated zero-value check.

## Proof of Concept
1. Deploy victim contract `V` with a payout function using `payable(msg.sender).transfer(amount)` where `amount > 0`.
2. Deploy attacker contract `A` whose `receive()` attempts to call back into `V`'s withdrawal function.
3. Call `V`'s payout to `A`. In `run_call`, `value.is_zero() == false`, `gas_limit == 0` (as solc emits for `.transfer(amount)`), so the code takes `(false, _) => (true, ReentrancyProtection::AllowReentry)` — `add_stipend = true` but no explicit reentrancy guard.
4. Since `metering/math.rs::new_nested_meter` grants the same `determine_call_stipend::<T>()` weight regardless of `AllowNext` vs `AllowReentry`, if that weight suffices for `A.receive()` to make a further call, `A` reenters `V` and drains funds — reproducible via a Rust integration test analogous to `evm_call_stipends_work_for_transfer_zero`/`testTransferReentrancy` in `Stipends.sol`, but using `amount > 0` instead of `0`.

### Citations

**File:** substrate/frame/revive/src/vm/evm/instructions/contract.rs (L193-203)
```rust
	let (add_stipend, reentracy) =
		match (value.is_zero(), gas_limit.try_into().is_ok_and(|limit: u64| limit == CALL_STIPEND))
		{
			(false, _) => (true, ReentrancyProtection::AllowReentry),
			// Heuristic: detect when solc passes `gas_limit = 2300` (the call stipend).
			// For zero-value transfer/send, solc injects `gas_limit = 2300` explicitly.
			// We apply `AllowNext` reentrancy protection and set `add_stipend = true` since the
			// raw 2300 gas value is only meaningful at Ethereum's gas scale.
			(_, true) => (true, ReentrancyProtection::AllowNext),
			(_, _) => (false, ReentrancyProtection::AllowReentry),
		};
```

**File:** prdoc/stable2603/pr_11227.prdoc (L6-12)
```text
    \ handles the 2300 gas stipend\n\n| Solidity call | value | gas passed by compiler\
    \ | Stipend source |\n|---|---|---|---|\n| `target.transfer(amount)` | > 0 | `0`\
    \ | EVM adds 2300 automatically |\n| `target.send(amount)` | > 0 | `0` | EVM adds\
    \ 2300 automatically |\n| `target.transfer(0)` | 0 | `2300` | Compiler injects\
    \ explicitly |\n| `target.send(0)` | 0 | `2300` | Compiler injects explicitly\
    \ |\n| `target.call{value: v}(\"\")` | any | remaining gas | No stipend (forwards\
    \ all gas) |\n\nThe zero-value case is the one detected by our `gas_limit == CALL_STIPEND`\
```

**File:** substrate/frame/revive/src/metering/math.rs (L374-391)
```rust
				CallResources::Ethereum { gas, add_stipend } => {
					let gas_limit = SignedGas::from_ethereum_gas(*gas);

					let (gas_limit, stipend) = if *add_stipend {
						let weight_stipend = determine_call_stipend::<T>();
						if weight_left.any_lt(weight_stipend) {
							return Err(<Error<T>>::OutOfGas.into());
						}

						(
							gas_limit.saturating_add(&SignedGas::<T>::from_weight_fee(
								T::FeeInfo::weight_to_fee(&weight_stipend),
							)),
							Some(weight_stipend),
						)
					} else {
						(gas_limit, None)
					};
```

**File:** substrate/frame/revive/src/exec.rs (L134-141)
```rust
	/// Activate reentrancy protection where the direct callee can be the same contract as the
	/// caller but none of the recursive callees of the callee must be the caller.
	///
	/// This is used for calls that transfer value but restrict gas so that the callee only has a
	/// stipend gas amount. In Ethereum that is not sufficient for the callee to make another call.
	/// However, due to gas scale differences that guarantee does not automatically hold in revive
	/// and we enforce it explicitly here.
	AllowNext,
```
