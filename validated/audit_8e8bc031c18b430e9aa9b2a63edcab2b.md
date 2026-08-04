### Title
ERC20 asset-precompile state-changing calls do not reject non-zero native `value`, permanently locking mistakenly-sent native funds - (File: `substrate/frame/assets/precompiles/src/lib.rs`)

### Summary
The `pallet-assets` ERC20 precompile (`ERC20<Runtime, PrecompileConfig, Instance>`) exposes Solidity-style `transfer`, `transferFrom`, `approve`, and `permit` entry points that operate purely on `pallet_assets` balances. None of these handlers check that the native `value` attached to the call (the pallet-revive analog of Solidity `msg.value`) is zero before proceeding, mirroring exactly the Li.Fi bug class where ERC20-only bridging functions never validated `msg.value == 0`.

### Finding Description
`ERC20::call` dispatches on the decoded `IERC20Calls` variant and only guards against delegate-calls and read-only state changes; it never inspects `env.value_transferred()`/the call's native value before running `Self::transfer`, `Self::approve`, `Self::transfer_from`, or `Self::permit`: [1](#0-0) 

Meanwhile, `pallet_revive`'s core execution path (`Stack::run`) unconditionally moves the native value attached to a call into the callee account **before** the callee's logic (including precompile logic) executes, regardless of whether the callee is a real contract, an EOA, or a precompile: [2](#0-1) 

The subsequent branch that mints the minimum balance / adds a consumer reference to keep a precompile's account alive is gated on `precompile.has_contract_info()`: [3](#0-2) 

For the ERC20 asset precompile, `HAS_CONTRACT_INFO` is `false`: [4](#0-3) 

That means the precompile's synthetic address (derived deterministically from the asset id via `AddressMatcher::Prefix`/`InlineAssetIdExtractor`/`ForeignAssetIdExtractor`) has no associated contract logic, no owner-controlled key, and — critically — no code path anywhere in the precompile implementation that can move a native balance back out of that account. Any native value a caller attaches to a `transfer`/`approve`/`transferFrom`/`permit` call against the ERC20 precompile is transferred by `transfer_from_origin` into this synthetic account and then simply sits there, since:
- The precompile never checks or rejects `value != 0`.
- The precompile has no "receive"/"withdraw" function analogous to an EOA or payable contract.
- No consumer reference or contract info exists for this address to route the balance anywhere.

This is structurally identical to the reported bug: functions whose entire purpose is a non-native (ERC20) transfer never validate that no native value was attached, so mistakenly-sent native funds become frozen on a balance that cannot be reached again through the public interface.

### Impact Explanation
Any unprivileged caller who mistakenly (or through buggy client/wallet tooling that defaults to attaching value, as happened in the referenced Li.Fi incident) sends native currency alongside a `transfer`, `transferFrom`, `approve`, or `permit` call to any `pallet-assets` ERC20 precompile address permanently loses that native balance — it is credited to an address with no reachable spending path via the precompile's public interface, matching the "permanent user-fund lock" impact category for this program.

### Likelihood Explanation
Likelihood is realistic but not universal: this requires a normal, unprivileged EOA/dApp caller to attach non-zero native value to what should be a value-free asset-transfer call — exactly the class of user/tooling mistake (e.g., wallet or SDK defaulting `msg.value`) that caused the real-world Li.Fi incident being used as the seed. No malicious peer, relayer, governance, or privileged actor is required — only a normal caller of `pallet_revive::Pallet::call`/`bare_call` targeting the precompile's H160 address with `value > 0`.

### Recommendation
In `substrate/frame/assets/precompiles/src/lib.rs`, add a guard in `ERC20::call` (or in each of `transfer`, `approve`, `transfer_from`, `permit`) that reverts with a clear error (e.g., `Error::Revert(Revert { reason: "Native value not accepted".into() })`) whenever `env.value_transferred()` (or the equivalent accessor available on `Ext`) is non-zero for these entry points. The same guard should be considered for other `HAS_CONTRACT_INFO = false` precompiles (e.g. `pallet-asset-conversion` and `pallet-xcm` precompiles) that similarly lack a way to reclaim native value credited to their synthetic account.

### Proof of Concept
1. Deploy/enable the `pallet-assets` ERC20 precompile for an asset id, giving it an address `P` per `AssetPrecompileConfig::MATCHER` (e.g., `InlineIdConfig`/`ForeignIdConfig`).
2. From a funded EOA, call `pallet_revive::Pallet::<Runtime>::call` (or `bare_call`) targeting address `P` with `input = IERC20::transferCall { to, value }.abi_encode()` and a non-zero native `value` (the call's own native-value parameter, distinct from the ERC20 `value` field).
3. Observe: `Stack::run` executes `transfer_from_origin` and moves the attached native value into `P`'s underlying `AccountId` before `ERC20::transfer` runs; the ERC20 transfer of `pallet_assets` tokens succeeds normally (no revert), and the call returns `true`.
4. Verify the native balance now sitting at `P`'s `AccountId` is unreachable: there is no `payable`/withdraw entry point in `ERC20<Runtime, ...>::call`, and `HAS_CONTRACT_INFO = false` means no contract exists there to ever move it — the funds are permanently stranded.

### Citations

**File:** substrate/frame/assets/precompiles/src/lib.rs (L158-162)
```rust
	type T = Runtime;
	type Interface = IERC20::IERC20Calls;
	const MATCHER: AddressMatcher = PrecompileConfig::MATCHER;
	const HAS_CONTRACT_INFO: bool = false;

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
