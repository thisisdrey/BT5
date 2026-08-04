[1](#0-0) [2](#0-1) [3](#0-2)

### Citations

**File:** substrate/frame/staking-async/src/pallet/mod.rs (L2011-2012)
```rust
				let era =
					session_rotation::Rotator::<T>::active_era().saturating_add(unbond_duration);
```

**File:** substrate/frame/treasury/src/lib.rs (L739-751)
```rust
			let now = T::BlockNumberProvider::current_block_number();
			ensure!(now >= spend.valid_from, Error::<T, I>::EarlyPayout);
			ensure!(spend.expire_at > now, Error::<T, I>::SpendExpired);
			ensure!(
				matches!(spend.status, PaymentState::Pending | PaymentState::Failed),
				Error::<T, I>::AlreadyAttempted
			);

			let id = T::Paymaster::pay(&spend.beneficiary, spend.asset_kind.clone(), spend.amount)
				.map_err(|_| Error::<T, I>::PayoutError)?;

			spend.status = PaymentState::Attempted { id };
			spend.expire_at = now.saturating_add(T::PayoutPeriod::get());
```

**File:** substrate/frame/bounties/src/lib.rs (L804-807)
```rust
				if let BountyStatus::PendingPayout { curator, beneficiary, unlock_at } =
					bounty.status
				{
					ensure!(Self::treasury_block_number() >= unlock_at, Error::<T, I>::Premature);
```
