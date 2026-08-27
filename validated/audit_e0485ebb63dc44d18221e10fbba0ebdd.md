#No Vulnerability found for this question.

Analysis: `append_action_add_key_with_full_access` in [1](#0-0)  is a `ReceiptManager` helper that appends an `AddKey` action with `FullAccess` permission to an outgoing action receipt being built by a `FunctionCall`'s host functions (e.g. `promise_batch_action_add_key_with_full_access`). It is never invoked from the Stake-action processing path.

The actual `Stake` action handler is `action_stake` in [2](#0-1) , dispatched from `apply_action` in [3](#0-2) . This function independently enforces the `amount`/`locked` invariant:
- A zero-stake action on an account with no existing lock is rejected via `TriesToUnstake` [4](#0-3) .
- A stake amount exceeding `account.amount() + account.locked()` fails the `checked_sub` and returns `TriesToStake` without mutating state [5](#0-4) .
- `amount`/`locked` are only mutated together (`set_amount`/`set_locked`) when `stake.stake > account.locked()`, and always by the exact same `increment`, preserving `amount + locked` [6](#0-5) .

No code path connects a `DeployContract` + `Stake` action list to `receipt_manager.rs::append_action_add_key_with_full_access`; that function is reachable only through VM host-function calls made from an already-executing `FunctionCall` action, not from `Stake` action dispatch. There is no mechanism by which submitting a Stake action for `0`, `1 yoctoNEAR`, or an amount exceeding balance can invoke this receipt-manager helper or otherwise desynchronize `locked` from the protocol's stake accounting — the balance/locked invariant is enforced entirely within `action_stake` via `checked_sub` and the `stake > locked` gate, both unconditioned on any DeployContract action in the same batch. The premise linking these two unrelated code paths does not hold in this codebase.

### Citations

**File:** runtime/runtime/src/receipt_manager.rs (L544-557)
```rust
    pub(super) fn append_action_add_key_with_full_access(
        &mut self,
        receipt_index: ReceiptIndex,
        public_key: PublicKey,
        nonce: Nonce,
    ) {
        self.append_action(
            receipt_index,
            Action::AddKey(Box::new(AddKeyAction {
                public_key,
                access_key: AccessKey { nonce, permission: AccessKeyPermission::FullAccess },
            })),
        );
    }
```

**File:** runtime/runtime/src/actions.rs (L59-109)
```rust
pub(crate) fn action_stake(
    account: &mut Account,
    result: &mut ActionResult,
    account_id: &AccountId,
    stake: &StakeAction,
    last_block_hash: &CryptoHash,
    epoch_info_provider: &dyn EpochInfoProvider,
) -> Result<(), RuntimeError> {
    let increment = stake.stake.saturating_sub(account.locked());

    if let Some(new_balance) = account.amount().checked_sub(increment) {
        if account.locked().is_zero() && stake.stake.is_zero() {
            // if the account hasn't staked, it cannot unstake
            result.result =
                Err(ActionErrorKind::TriesToUnstake { account_id: account_id.clone() }.into());
            return Ok(());
        }

        if stake.stake > Balance::ZERO {
            let minimum_stake = epoch_info_provider.minimum_stake(last_block_hash)?;
            if stake.stake < minimum_stake {
                result.result = Err(ActionErrorKind::InsufficientStake {
                    account_id: account_id.clone(),
                    stake: stake.stake,
                    minimum_stake,
                }
                .into());
                return Ok(());
            }
        }

        result.validator_proposals.push(ValidatorStake::new(
            account_id.clone(),
            stake.public_key.clone(),
            stake.stake,
        ));
        if stake.stake > account.locked() {
            // We've checked above `account.amount >= increment`
            account.set_amount(new_balance);
            account.set_locked(stake.stake).or_inconsistent_state(account_id)?;
        }
    } else {
        result.result = Err(ActionErrorKind::TriesToStake {
            account_id: account_id.clone(),
            stake: stake.stake,
            locked: account.locked(),
            balance: account.amount(),
        }
        .into());
    }
    Ok(())
```

**File:** runtime/runtime/src/lib.rs (L680-690)
```rust
            Action::Stake(stake) => {
                metrics::ACTION_CALLED_COUNT.stake.inc();
                action_stake(
                    account.as_mut().expect(EXPECT_ACCOUNT_EXISTS),
                    &mut result,
                    account_id,
                    stake,
                    &apply_state.prev_block_hash,
                    epoch_info_provider,
                )?;
            }
```
