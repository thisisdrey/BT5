Audit Report

## Title
Native/EVM value sent with `CALL` to a stateless (`HAS_CONTRACT_INFO = false`) `pallet-revive` precompile is permanently locked - ([File: substrate/frame/revive/src/exec.rs])

## Summary
`Stack::run` in `pallet-revive` performs `Self::transfer_from_origin` for the callee's account unconditionally for every non-delegate frame, before the callee (contract or precompile) executes, and only special-cases account/consumer bookkeeping for precompiles with `has_contract_info() == true`. Built-in Ethereum precompiles (`ECRecover`, `Sha256`, `Ripemd160`, `Identity`, `Modexp`, `Bn128Add/Mul/Pairing`, `Blake2F`, `System`, etc.) are declared with `HAS_CONTRACT_INFO = false`, and their `call()` implementations never move, forward, or refund a received `value`, so any native value sent via `CALL`/`eth_call` with `value > 0` to one of these fixed addresses becomes permanently stranded.

## Finding Description
The balance transfer for a call frame happens unconditionally whenever `frame.delegate.is_none()`: [1](#0-0) 

Immediately after, the account/ED-minting/consumer bookkeeping is gated strictly behind `precompile.has_contract_info()`: [2](#0-1) 

The `Precompile` trait explicitly documents that `HAS_CONTRACT_INFO = false` means "No account or any other state will be created for the address," and only `call()` (not `call_with_info()`, which is where value/deposit-aware logic would live) is implemented for such precompiles — yet the transfer into that address's balance via `transfer_from_origin` still runs regardless of this flag. `System` and all classic Ethereum-compatibility precompiles (`ecrecover.rs`, `sha256.rs`, `ripemd160.rs`, `identity.rs`, `modexp.rs`, `bn128.rs`, `blake2f.rs`) declare `HAS_CONTRACT_INFO = false`, confirmed by grepping the `substrate/frame/revive/src/precompiles/builtin/` directory. None of these `call()` bodies reference or forward `value`.

The repository's own test `pure_precompile_works` in `substrate/frame/revive/src/tests/pvm.rs` calls each of these precompiles with `value = 100` and explicitly asserts the balance accumulates at the precompile address afterward rather than being rejected or refunded, which corroborates the described behavior in-repo.

By contrast, the codebase does have a precedent guard for a similar situation: `into_call` in `substrate/frame/revive/src/evm/call.rs` explicitly rejects non-zero `value` sent to `RUNTIME_PALLETS_ADDR` ("Runtime pallets address cannot be called with value") — but this guard is not applied to precompile addresses with `HAS_CONTRACT_INFO = false`, and more importantly it only covers the top-level `eth_call`/RLP transaction decode path, not the internal `CALL` opcode path inside `Stack::run` used by any contract that itself issues a low-level call with value to a precompile address. Since these precompile addresses (`0x01`–`0x09`, `0x900`, etc.) are protocol-fixed and have no corresponding private key, no `ContractInfo`, and no `terminate()`/self-destruct path, once `value` lands there via `transfer_from_origin` it cannot be moved out by any subsequent transaction — this is a genuine, deterministic, permanent balance lock reachable by any unprivileged account issuing an ordinary `CALL`/`eth_call` with `value > 0`.

## Impact Explanation
This matches the "permanent user-fund lock" category in the impact gate: an unprivileged, ordinary account (EOA or contract) can permanently lose native/EVM value with no recovery path, purely by directing value to a fixed, well-known address (e.g., precompile `0x01`–`0x09`), which is exactly the kind of address naive/ported EVM tooling and scripts reference. No privileged actor, validator, or off-chain assumption is required.

## Likelihood Explanation
The bug is 100% deterministic and reproducible on every call with `value > 0` targeting a `HAS_CONTRACT_INFO = false` precompile; it requires only a normal `CALL`/`eth_call` from any account, no special privileges, and the repository's own test (`pure_precompile_works`) documents this exact balance-accumulation behavior as expected rather than treating it as an error condition.

## Recommendation
- In `Stack::run` (`substrate/frame/revive/src/exec.rs`), before/instead of invoking `Self::transfer_from_origin` for a target that resolves to a precompile with `has_contract_info() == false`, reject the call when `frame.value_transferred` is non-zero (mirroring the existing `RUNTIME_PALLETS_ADDR` value-rejection guard in `substrate/frame/revive/src/evm/call.rs`), or have `transfer_from_origin` skip/refuse transfers to such destinations.
- Update `pure_precompile_works` and any other tests currently asserting balance accumulation at stateless precompiles to instead assert the call reverts (or that value is refunded) once the fix lands.

## Proof of Concept
1. On a `pallet-revive`-enabled chain, deploy/use a contract that issues a low-level `CALL` with `value = N > 0` to `0x0000000000000000000000000000000000000001` (`ECRecover`) with valid ABI-encoded input, as done in `pure_precompile_works` (`substrate/frame/revive/src/tests/pvm.rs`).
2. Observe the call succeeds, returns the expected `ECRecover` output, and `Pallet::<T>::evm_balance(&precompile_addr)` equals `N`.
3. Confirm there is no private key, `ContractInfo`, or `terminate()`/sweep path for that address — the `N` value is permanently unrecoverable, as demonstrated by the existing assertions in `pure_precompile_works`.

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

**File:** substrate/frame/revive/src/exec.rs (L1389-1405)
```rust
			// We need to make sure that the pre-compiles contract exist before executing it.
			// A few more conditionals:
			// 	- Only contracts with extended API (has_contract_info) are guaranteed to have an
			//    account.
			//  - Only when not delegate calling we are executing in the context of the pre-compile.
			//    Pre-compiles itself cannot delegate call.
			if let Some(precompile) = executable.as_precompile() &&
				precompile.has_contract_info() &&
				frame.delegate.is_none() &&
				!<System<T>>::account_exists(account_id)
			{
				// prefix matching pre-compiles cannot have a contract info
				// hence we only mint once per pre-compile
				T::Currency::mint_into(account_id, T::Currency::minimum_balance())?;
				// make sure the pre-compile does not destroy its account by accident
				<System<T>>::inc_consumers(account_id)?;
			}
```
