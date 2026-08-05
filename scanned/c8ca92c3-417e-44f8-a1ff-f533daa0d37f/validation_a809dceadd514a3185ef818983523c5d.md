## Analysis

I found a strong local analog to the "missing deadline" bug class: Ethereum-style transactions dispatched through `pallet-revive` (including calls into the `pallet-asset-conversion` precompile's swap functions) are **hardcoded as immortal**, meaning they can sit in the transaction pool indefinitely and be executed at an arbitrarily later block, at which point on-chain reserves may have moved far from the price the signer expected — exactly the scenario the original report warns about, but structurally guaranteed rather than incidental.

### Title
Ethereum-style Extrinsics in pallet-revive Are Hardcoded Immortal, Allowing Stale AMM Swap Execution With No Deadline Protection - (File: `cumulus/parachains/runtimes/assets/asset-hub-westend/src/lib.rs`)

### Summary
`EthExtraImpl::get_eth_extension` — the transaction-extension builder used for all Ethereum-formatted extrinsics dispatched via `pallet-revive`/`eth-rpc` — always constructs `frame_system::CheckMortality::from(generic::Era::Immortal)`, regardless of caller input. This removes any block-height-bounded expiry for EVM-origin transactions, including calls into the `pallet-asset-conversion` precompile's `swapExactTokensForTokens`/`swapTokensForExactTokens` entry points, which — like the vulnerable `convert()`/`buyPortalEnergy()`/`sellPortalEnergy()` in the external report — only protect against price movement via a static `amountOutMin`/`amountInMax`, with no deadline parameter at all.

### Finding Description
The extrinsic-level swap entry points in `substrate/frame/asset-conversion/src/lib.rs` (`swap_exact_tokens_for_tokens`, `swap_tokens_for_exact_tokens`) and their underlying logic (`do_swap_exact_tokens_for_tokens`, `do_swap_tokens_for_exact_tokens` at lines 980-1063) only validate `amount_out_min`/`amount_in_max` against current pool reserves at execution time: [1](#0-0) 

There is no deadline/expiry parameter tied to wall-clock or block-height freshness of the *signer's intent* — only a price bound.

For normal signed Substrate extrinsics, this gap is normally mitigated because `frame_system::CheckMortality` bounds how long a transaction can remain valid in the pool (`Era::mortal(period, phase)`), acting as an implicit "deadline." However, for Ethereum-formatted transactions dispatched through `pallet-revive` (the path used to call the `IAssetConversion` precompile at `substrate/frame/asset-conversion/precompiles/src/lib.rs`), the extension builder hardcodes an **immortal** era unconditionally: [2](#0-1) 

The same pattern appears in the revive dev-node runtime: [3](#0-2) 

`CheckMortality::validate` derives the transaction pool `longevity` directly from the `Era`; for `Era::Immortal`, `death()` returns `u64::MAX`, so the pool never treats the transaction as stale on mortality grounds: [4](#0-3) [5](#0-4) 

The precompile that exposes the swap to EVM callers dispatches straight into the pallet's `Swap` trait implementation with only `amountOutMin`/`amountInMax`, no deadline: [6](#0-5) [7](#0-6) 

Because the outer Ethereum transaction is immortal by construction (independent of anything the caller sets), a low-fee EVM swap can remain valid in the pool for an unbounded number of blocks and be included whenever fee-market conditions make it attractive to a block author — at which point pool reserves may have shifted dramatically. The only remaining guard, `amount_out_min`/`amount_in_max`, bounds worst-case slippage per-trade but does not protect against the signer's price assumption becoming stale, which is precisely the invariant the external report identifies as broken.

### Impact Explanation
Any user submitting an EVM-formatted swap call to the `AssetConversion` precompile through `pallet-revive`/`eth-rpc` (e.g., on Asset Hub or any chain enabling this precompile) has no way to bound how long their swap intent remains executable, unlike native Substrate signed extrinsics which get a bounded `Era::mortal` lifetime. This is a public-entrypoint, unprivileged-attacker-relevant condition (no malicious validator, relayer, or admin is required) because ordinary fee-market dynamics or intentional low-fee submission are sufficient to leave the transaction pending indefinitely, after which normal, permissionless block inclusion executes it against a since-diverged pool state, mirroring exactly the described bug class: no deadline mechanism to invalidate stale swap intents.

### Likelihood Explanation
High: `Era::Immortal` is unconditionally hardcoded in `get_eth_extension` for every EVM transaction routed through `pallet-revive`'s Ethereum compatibility layer — this is not a corner case but the default and only behavior for this transaction format. Any EVM caller of the `AssetConversion` precompile's swap functions is affected without any special conditions.

### Recommendation
Either (a) stop hardcoding `Era::Immortal` for Ethereum-formatted transactions and instead derive a bounded mortality window analogous to native extrinsics, or (b) add an explicit `deadline` (block number or timestamp) parameter to `swap_exact_tokens_for_tokens`/`swap_tokens_for_exact_tokens` (and their `SwapCredit`/precompile counterparts) that is checked against `frame_system::Pallet::<T>::block_number()` at dispatch time, independent of transaction-pool mortality, so stale swap intents are rejected regardless of the outer extrinsic's `Era`.

### Proof of Concept
1. A user signs an Ethereum-formatted transaction calling `IAssetConversion.swapExactTokensForTokens(path, amountIn, amountOutMin, sendTo, keepAlive)` via `pallet-revive`'s eth-rpc, with `amountOutMin` set based on the current pool price.
2. Because `EthExtraImpl::get_eth_extension` sets `CheckMortality::from(Era::Immortal)` (`cumulus/parachains/runtimes/assets/asset-hub-westend/src/lib.rs:1784`), the transaction never becomes "stale" from the pool's perspective due to mortality — it can be resubmitted/gossiped/included at any future block as long as the nonce is still valid.
3. The user sets a low tip, so the transaction is not immediately included and sits in the pool for an extended number of blocks while pool reserves shift due to other trading activity.
4. A block author later includes the transaction. `do_swap_exact_tokens_for_tokens` (`substrate/frame/asset-conversion/src/lib.rs:980-1014`) only checks `amount_out >= amount_out_min` against the *current* reserves — the swap executes at the new, possibly much worse, price, satisfying the minimum but delivering an outcome the signer never intended given how much time and price divergence has passed, with no on-chain deadline check available to prevent it.

### Citations

**File:** substrate/frame/asset-conversion/src/lib.rs (L996-1002)
```rust
			let amount_out = path.last().map(|(_, a)| *a).ok_or(Error::<T>::InvalidPath)?;
			if let Some(amount_out_min) = amount_out_min {
				ensure!(
					amount_out >= amount_out_min,
					Error::<T>::ProvidedMinimumNotSufficientForSwap
				);
			}
```

**File:** cumulus/parachains/runtimes/assets/asset-hub-westend/src/lib.rs (L1777-1799)
```rust
	fn get_eth_extension(nonce: u32, tip: Balance) -> Self::ExtensionV0 {
		(
			frame_system::AuthorizeCall::<Runtime>::new(),
			frame_system::CheckNonZeroSender::<Runtime>::new(),
			frame_system::CheckSpecVersion::<Runtime>::new(),
			frame_system::CheckTxVersion::<Runtime>::new(),
			frame_system::CheckGenesis::<Runtime>::new(),
			frame_system::CheckMortality::from(generic::Era::Immortal),
			frame_system::CheckNonce::<Runtime>::from(nonce),
			frame_system::CheckWeight::<Runtime>::new(),
			pallet_pgas_allowance::ChargePGAS::<
				Runtime,
				pallet_asset_conversion_tx_payment::ChargeAssetTxPayment<Runtime>,
			>::new_skip_pgas(
				pallet_asset_conversion_tx_payment::ChargeAssetTxPayment::<Runtime>::from(
					tip, None,
				),
			),
			frame_metadata_hash_extension::CheckMetadataHash::<Runtime>::new(false),
			pallet_revive::evm::tx_extension::SetOrigin::<Runtime>::new_from_eth_transaction(),
		)
			.into()
	}
```

**File:** substrate/frame/revive/dev-node/runtime/src/lib.rs (L205-218)
```rust
	fn get_eth_extension(nonce: u32, tip: Balance) -> Self::ExtensionV0 {
		(
			frame_system::CheckNonZeroSender::<Runtime>::new(),
			frame_system::CheckSpecVersion::<Runtime>::new(),
			frame_system::CheckTxVersion::<Runtime>::new(),
			frame_system::CheckGenesis::<Runtime>::new(),
			frame_system::CheckMortality::from(sp_runtime::generic::Era::Immortal),
			frame_system::CheckNonce::<Runtime>::from(nonce),
			frame_system::CheckWeight::<Runtime>::new(),
			pallet_transaction_payment::ChargeTransactionPayment::<Runtime>::from(tip),
			pallet_revive::evm::tx_extension::SetOrigin::<Runtime>::new_from_eth_transaction(),
			frame_system::WeightReclaim::<Runtime>::new(),
		)
	}
```

**File:** substrate/primitives/runtime/src/generic/era.rs (L93-99)
```rust
	/// Get the block number of the first block at which the era has ended.
	pub fn death(self, current: u64) -> u64 {
		match self {
			Self::Immortal => u64::MAX,
			Self::Mortal(period, _) => self.birth(current) + period,
		}
	}
```

**File:** substrate/frame/system/src/extensions/check_mortality.rs (L87-107)
```rust
	fn validate(
		&self,
		origin: <T as Config>::RuntimeOrigin,
		_call: &T::RuntimeCall,
		_info: &DispatchInfoOf<T::RuntimeCall>,
		_len: usize,
		_self_implicit: Self::Implicit,
		_inherited_implication: &impl Encode,
		_source: TransactionSource,
	) -> ValidateResult<Self::Val, T::RuntimeCall> {
		let current_u64 = <Pallet<T>>::block_number().saturated_into::<u64>();
		let valid_till = self.0.death(current_u64);
		Ok((
			ValidTransaction {
				longevity: valid_till.saturating_sub(current_u64),
				..Default::default()
			},
			(),
			origin,
		))
	}
```

**File:** substrate/frame/asset-conversion/precompiles/src/lib.rs (L56-70)
```rust
	interface IAssetConversion {
		/// Swap an exact amount of input tokens for as many output tokens as possible.
		/// @param path Ordered list of SCALE-encoded asset identifiers defining the swap route.
		/// @param amountIn Exact amount of the first asset to swap.
		/// @param amountOutMin Minimum acceptable amount of the last asset to receive.
		/// @param sendTo Address to receive the output tokens.
		/// @param keepAlive If true, ensures the sender account stays above existential deposit.
		/// @return amountOut The amount of output tokens received.
		function swapExactTokensForTokens(
			bytes[] calldata path,
			uint256 amountIn,
			uint256 amountOutMin,
			address sendTo,
			bool keepAlive
		) external returns (uint256 amountOut);
```

**File:** substrate/frame/asset-conversion/precompiles/src/lib.rs (L289-319)
```rust
	fn swap_exact_tokens_for_tokens(
		call: &IAssetConversion::swapExactTokensForTokensCall,
		env: &mut impl Ext<T = Runtime>,
	) -> Result<Vec<u8>, Error> {
		let path_len = Self::validated_path_len(&call.path)?;
		env.charge(
			<Runtime as pallet_asset_conversion::Config>::WeightInfo::swap_exact_tokens_for_tokens(
				path_len,
			),
		)?;
		let path: Vec<_> =
			call.path.iter().map(|e| Self::decode_asset_kind(e)).collect::<Result<_, _>>()?;

		let sender = Self::caller_account_id(env)?;
		let send_to = env.to_account_id(&H160(call.sendTo.0 .0));

		let amount_out = <pallet_asset_conversion::Pallet<Runtime> as Swap<
			<Runtime as frame_system::Config>::AccountId,
		>>::swap_exact_tokens_for_tokens(
			sender,
			path,
			Self::to_balance(call.amountIn)?,
			Some(Self::to_balance(call.amountOutMin)?),
			send_to,
			call.keepAlive,
		)?;

		Ok(IAssetConversion::swapExactTokensForTokensCall::abi_encode_returns(&Self::to_u256(
			amount_out,
		)?))
	}
```
