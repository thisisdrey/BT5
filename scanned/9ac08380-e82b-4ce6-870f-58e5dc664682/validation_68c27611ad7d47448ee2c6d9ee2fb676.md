### Title
Native value sent alongside non-payable ERC20 precompile calls (`balanceOf`, `totalSupply`, `allowance`, `name`, `symbol`, `decimals`, `nonces`, `permit`, `DOMAIN_SEPARATOR`) is silently transferred and permanently trapped - (File: `substrate/frame/assets/precompiles/src/lib.rs`)

### Summary
The external report's core defect is: a public entry point accepts native value unconditionally (`payable`), but the handler logic never consumes, forwards, or refunds that value, so it becomes permanently stuck. The local analog is the `ERC20` precompile dispatch in `pallet-revive`/`pallet-assets-precompiles`: value transfer to a precompile's account happens unconditionally before the specific Solidity selector is dispatched, but several of the dispatched functions are pure/view operations that never use, return, or move the transferred balance, and the precompile address has no mechanism to reclaim it.

### Finding Description
In `pallet-revive`'s execution engine, any call frame targeting a precompile unconditionally transfers `frame.value_transferred` to the target account before the precompile logic runs: [1](#0-0) 

This transfer is performed regardless of which Solidity function selector is being invoked. The `ERC20` precompile implementation then dispatches on the decoded call without any check that value is zero for non-mutating selectors — it only special-cases read-only *state changes* for mutating functions (`transfer`, `approve`, `transferFrom`, `permit`), not value handling: [2](#0-1) 

Functions such as `totalSupply`, `balanceOf`, `allowance`, `name`, `symbol`, `decimals`, `nonces`, and `DOMAIN_SEPARATOR` are pure read operations in the Solidity ABI (`IERC20`/`IERC20Metadata`/EIP-2612), i.e. they are not meant to be payable. Yet nothing in the dispatch path rejects a non-zero `value` for these selectors — the transfer already happened in `exec.rs` before the match statement executes. The precompile has `HAS_CONTRACT_INFO: bool = false`, meaning it has no contract-info-backed account lifecycle, no `receive`/fallback semantics, and no self-destruct/beneficiary path (unlike ordinary contracts, cf. the termination beneficiary logic in `substrate/frame/contracts/src/storage/meter.rs:596-605`). There is therefore no way for a caller (or anyone) to retrieve value accidentally sent to the precompile's fixed address while invoking a view/pure selector.

A related fix attempt is documented in `prdoc/stable2512/pr_10080.prdoc`, titled "precompiles: Enforce state mutability," which explicitly states that `pallet-assets-precompile`, `pallet-xcm-precompiles`, and revive builtin precompiles "currently violate Solidity state mutability... potentially introducing a new attack vector," and that enforcing this at the `Precompile` trait level is not feasible because mutability is determined by the Solidity function selector, not by a trait-level constant: [3](#0-2) 

This confirms upstream awareness of exactly this class of defect (mismatch between declared Solidity mutability — including `payable` vs non-payable — and actual runtime enforcement), but the enforcement point observed in the dispatch code (`is_read_only` checks in `substrate/frame/assets/precompiles/src/lib.rs:176-185`) only guards against *state mutation during read-only calls*; it does not guard against *non-zero value being accepted by non-payable selectors*.

### Impact Explanation
Any EVM-style caller (from a contract or via `eth_call`/`eth_transact`-style user transaction) that attaches native value to a call targeting a view/pure ERC20 precompile selector (e.g. `balanceOf(address)`) will have that value transferred to the precompile's fixed address by `transfer_from_origin`, with no code path in the precompile implementation to use, forward, or refund it. Because the precompile account is not a normal contract (no code, no receive/fallback, no owner-controlled withdrawal, no self-destruct beneficiary), the funds are permanently locked — an unrecoverable fund loss for the unprivileged caller, matching "permanent user-fund lock" in the impact gate.

### Likelihood Explanation
This requires only an ordinary unprivileged EVM-style call with non-zero `value` targeting one of the read-only ERC20 selectors — no relayer, validator, governance, or privileged actor involved. It can be triggered accidentally (e.g. a wallet/tooling bug that attaches value to a `balanceOf` call, similar to the original `requestERC20Service()` scenario) or deliberately by any user. The only friction is that the caller must go through the EVM/`pallet-revive` call path with an explicit non-zero `value` on a selector that is documented as non-payable in the Solidity interface but not enforced as such at the Rust dispatch layer.

### Recommendation
In the `ERC20::call` implementation (and other `Precompile` implementations that mix mutating/payable and pure/non-payable Solidity selectors), explicitly reject any call carrying non-zero `env.value_transferred()` when the matched selector corresponds to a non-payable Solidity function (all view/pure ERC20/ERC20Metadata/EIP-2612 getters). This should ideally be enforced generically at the precompile dispatch layer (as the `pr_10080.prdoc` intent suggests) by deriving expected mutability per-selector from the ABI and validating both "no state change on read-only calls" and "no value on non-payable calls" before invoking the handler, with a clean revert instead of a value transfer, so unprivileged callers cannot lose funds to a precompile that has no way to return them.

### Proof of Concept
1. Deploy/target the `ERC20` precompile for an existing asset (fixed address per `AssetIdExtractor`/`MATCHER`).
2. From a contract or via `bare_call`, invoke the precompile with selector `balanceOf(address)` while attaching a non-zero native `value` in the call (`env.call(... value: N ...)`), analogous to `CallSelfWithDust.sol`'s pattern of attaching value to a call: `this.f{value: 10}();` [4](#0-3) 
3. Execution flow: `exec.rs` transfers `value` (N) into the precompile's account via `transfer_from_origin` before executing the precompile body: [5](#0-4) 
4. The `ERC20::call` dispatch matches `IERC20Calls::balanceOf` and returns the balance without touching `value`: [6](#0-5) 
5. Result: the call succeeds, returns the queried balance, and `N` units of native currency remain permanently credited to the precompile's address with no dispatchable path to move them out — fund loss for the caller.

### Citations

**File:** substrate/frame/revive/src/exec.rs (L1375-1387)
```rust
			// Every non delegate call or instantiate also optionally transfers the balance.
			// If it is a delegate call, then we've already transferred tokens in the
			// last non-delegate frame.
			if frame.delegate.is_none() {
				Self::transfer_from_origin(
					&self.origin,
					&caller,
					account_id,
					frame.value_transferred,
					&mut frame.frame_meter,
					self.exec_config,
				)?;
			}
```

**File:** substrate/frame/assets/precompiles/src/lib.rs (L163-207)
```rust
	fn call(
		address: &[u8; 20],
		input: &Self::Interface,
		env: &mut impl Ext<T = Self::T>,
	) -> Result<Vec<u8>, Error> {
		frame_support::ensure!(
			!env.is_delegate_call(),
			pallet_revive::Error::<Self::T>::PrecompileDelegateDenied,
		);

		let asset_id = PrecompileConfig::AssetIdExtractor::asset_id_from_address(address)?.into();
		let contract_addr = H160::from(*address);

		match input {
			// State-changing calls - check read-only
			IERC20Calls::transfer(_) |
			IERC20Calls::approve(_) |
			IERC20Calls::transferFrom(_) |
			IERC20Calls::permit(_)
				if env.is_read_only() =>
			{
				Err(Error::Error(pallet_revive::Error::<Self::T>::StateChangeDenied.into()))
			},

			// ERC20 functions
			IERC20Calls::transfer(call) => Self::transfer(asset_id, call, env),
			IERC20Calls::totalSupply(_) => Self::total_supply(asset_id, env),
			IERC20Calls::balanceOf(call) => Self::balance_of(asset_id, call, env),
			IERC20Calls::allowance(call) => Self::allowance(asset_id, call, env),
			IERC20Calls::approve(call) => Self::approve(asset_id, call, env),
			IERC20Calls::transferFrom(call) => Self::transfer_from(asset_id, call, env),

			// ERC20Permit functions (EIP-2612)
			IERC20Calls::permit(call) => Self::permit(asset_id, contract_addr, call, env),
			IERC20Calls::nonces(call) => Self::nonces(contract_addr, call, env),
			IERC20Calls::DOMAIN_SEPARATOR(_) => {
				Self::domain_separator(asset_id, contract_addr, env)
			},

			// ERC20Metadata functions
			IERC20Calls::name(_) => Self::name(asset_id, env),
			IERC20Calls::symbol(_) => Self::symbol(asset_id, env),
			IERC20Calls::decimals(_) => Self::decimals(asset_id, env),
		}
	}
```

**File:** prdoc/stable2512/pr_10080.prdoc (L1-16)
```text
title: 'precompiles: Enforce state mutability'
doc:
- audience: Runtime Dev
  description: |-
    `pallet-assets-precompile`, `pallet-xcm-precompiles` and revive builtin precompile implementations currently violate [Solidity state mutability](https://docs.soliditylang.org/en/latest/grammar.html#syntax-rule-SolidityParser.stateMutability), potentially introducing a new attack vector. This PR implements corresponding checks at the function dispatch.

    Could be enforced in `pallet-revive`, however:
    1. Adding something like a `const MUTATES: bool` to the `Precompile` trait won't help because whether the call is mutating or not depends on the [Solidity function selector.](https://docs.soliditylang.org/en/latest/abi-spec.html#function-selector).
    2. Alloy, which we are using to parse the interface definitions prior to calling precompile implementations, doesn't provide a mapping from function selector to its mutability [modifier](https://docs.soliditylang.org/en/latest/cheatsheet.html#modifiers).
crates:
- name: pallet-assets-precompiles
  bump: patch
- name: pallet-xcm-precompiles
  bump: patch
- name: pallet-revive
  bump: patch
```

**File:** substrate/frame/revive/fixtures/contracts/call_self_with_dust.sol (L1-7)
```text
contract CallSelfWithDust {
    function f() external payable {}

    function call() public payable {
        this.f{value: 10}();
    }
}
```
