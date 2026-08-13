### Title
Borrowers can be liquidated immediately when a paused bank/protocol resumes, with no grace period - (File: `programs/marginfi/src/utils/general.rs`, `programs/marginfi/src/state/panic_state.rs`)

### Summary
Both the per-bank `BankOperationalState::Paused` mechanism and the group-wide `panic_pause`/`panic_unpause` mechanism block repayment and liquidation together, and resume them together, with no grace period for borrowers to restore health after the pause ends.

### Finding Description
Two independent pause layers in this codebase exhibit the exact bug class described in the report:

1. **Per-bank operational state.** `validate_bank_state` in `programs/marginfi/src/utils/general.rs` blocks both repay and liquidation while a bank is `Paused` (`InstructionKind::FailsInPausedState` is used for liquidation, and repay is blocked as part of `FailsIfPausedOrReduceState`/reduce-state rules) [1](#0-0) . This is documented explicitly in the bank-state guide: in the `Paused` state neither repay nor liquidate is allowed, and in `Operational` state both become allowed again [2](#0-1) . When the admin flips `operational_state` from `Paused` back to `Operational` via `configure()`, the transition is instantaneous and unconditional — there is no timestamp or cooldown recorded to delay liquidation eligibility after the flip [3](#0-2) .

2. **Group-wide panic pause.** The `panic_pause`/`panic_unpause` flow blocks all deposits, borrows, withdrawals, repayments, and classic liquidation while active, as documented in the permissions guide [4](#0-3) . `PanicStateImpl::unpause` simply clears the paused flag and resets the pause-start timestamp with no delay or grace window before normal operations (including liquidation) resume [5](#0-4) . `panic_unpause` calls this directly as soon as the admin (or expiration) ends the pause [6](#0-5) .

In both cases, while paused, an account's health can silently deteriorate due to market/oracle price movement (borrowers cannot repay to restore health, and the protocol's own tests confirm depositing/repaying is blocked during pause) [7](#0-6) . The moment the pause lifts, liquidation is immediately re-enabled with no buffer, matching the exact scenario from the external report: borrowers become instantly liquidatable by bots unless they can front-run the resumption, through no fault of their own.

### Impact Explanation
This causes borrowers to be unfairly and immediately liquidated the instant a pause (bank-level or protocol-level) is lifted, even though they had no opportunity to react to health deterioration during the pause window (repay/deposit being blocked is exactly what prevented them from protecting themselves). This is an unauthorized-state-change / unfair-loss-of-funds impact on unprivileged borrowers, consistent with the "no grace period" bug class, and is reachable through the core bank/liquidation pause paths without any admin collusion — any legitimate admin-triggered pause/unpause (e.g., incident response) creates this window.

### Likelihood Explanation
Medium likelihood: this requires a pause (per-bank `Paused` state or protocol-wide `panic_pause`) to have occurred while market prices moved against open borrow positions, followed by an unpause. Pausing is an expected, documented operational action (used for incident response or bank setup) rather than a rare edge case, and liquidator bots are expected to monitor and act instantly on any newly-unhealthy account, so the liquidation race is realistic once the state clears.

### Recommendation
Add a grace period after a bank transitions out of `Paused` (in `Bank::configure`) and after `panic_unpause`/auto-expiry (in `PanicStateImpl::unpause`/`unpause_if_expired`) during which liquidation is still blocked (but repay/deposit are allowed), giving borrowers time to restore health before liquidation bots can act. This mirrors the client's acknowledged recommendation in the original report.

### Proof of Concept
1. Bank is `Operational`; a borrower opens a healthy position.
2. Admin sets `operational_state = Paused` (or triggers `panic_pause`) — per `validate_bank_state`/`FailsInPausedState`/`FailsIfPausedOrReduceState`, both repay and liquidation are now rejected [8](#0-7) .
3. During the pause, oracle price of the borrower's collateral drops, making the position unhealthy. Borrower cannot repay or deposit more collateral to fix this (blocked by the same checks).
4. Admin resumes the bank (`operational_state = Operational` via `configure()`) or calls `panic_unpause` — both take effect immediately with no cooldown [9](#0-8) [5](#0-4) .
5. A liquidator bot immediately submits `LendingAccountLiquidate`/`start_liquidation`+`repay`+`end_liquidation` in the very next available transaction, liquidating the borrower before they can react.

### Citations

**File:** programs/marginfi/src/utils/general.rs (L251-309)
```rust
#[derive(Debug, Clone, Copy)]
pub enum InstructionKind {
    /// Only fails if the bank is in `BankKilledByBankruptcy`, technically doesn't exist (yet)
    Unrestricted,
    /// E.g. withdraw, repay
    FailsInReduceState,
    /// E.g. liquidation
    FailsInPausedState,
    /// E.g. borrow, deposit
    FailsIfPausedOrReduceState,
}

// TODO remove redundant checks for these elsewhere in the program (they are nested many laters deep
// in various value delta functions)
/// Validate the bank's state does not forbid the execution of an instruction
pub fn validate_bank_state(bank: &Bank, kind: InstructionKind) -> MarginfiResult {
    if bank.config.operational_state == BankOperationalState::KilledByBankruptcy {
        return err!(MarginfiError::BankKilledByBankruptcy);
    }
    // Bank exists but has not completed one-time setup (e.g. JupLend seed deposit). Block every
    // operation until init runs.
    if bank.config.operational_state == BankOperationalState::Uninitialized {
        return err!(MarginfiError::BankUninitialized);
    }

    match kind {
        InstructionKind::FailsInReduceState if bank.config.operational_state.is_reduce_only() => {
            return err!(MarginfiError::BankReduceOnly);
        }

        InstructionKind::FailsInPausedState
            if bank.config.operational_state == BankOperationalState::Paused =>
        {
            return err!(MarginfiError::BankPaused);
        }

        InstructionKind::FailsIfPausedOrReduceState
            if matches!(
                bank.config.operational_state,
                BankOperationalState::Paused
                    | BankOperationalState::ReduceOnly
                    | BankOperationalState::ReduceOnlyWithBorrowingPower
            ) =>
        {
            return match bank.config.operational_state {
                BankOperationalState::Paused => {
                    err!(MarginfiError::BankPaused)
                }
                state if state.is_reduce_only() => {
                    err!(MarginfiError::BankReduceOnly)
                }
                _ => unreachable!(),
            };
        }
        _ => {}
    }

    Ok(())
}
```

**File:** guides/ADMIN/BANK_STATE.md (L18-58)
```markdown
### Paused

All operations are halted. Users cannot deposit, borrow, withdraw, repay, or be liquidated. This is
the default state for newly created banks.

Use cases:
- Initial setup: configure the bank before allowing users to interact with it.
- Emergency: halt all activity on a bank while investigating an issue.

### Operational

Normal operations. All user actions are allowed: deposit, borrow, withdraw, repay, and liquidation.

### ReduceOnly

Only withdrawals and repayments are allowed. New deposits and borrows are blocked. This state is
intended for winding down a bank.

Important nuances for health calculations in ReduceOnly:
- **Initial margin**: assets in a ReduceOnly bank are valued at **$0**. This means users cannot
  open new borrows using ReduceOnly collateral.
- **Maintenance margin**: assets in a ReduceOnly bank retain their **full value**. This means
  existing positions are not immediately liquidatable just because a bank entered ReduceOnly.

This asymmetry is by design: the system prevents new risk from being taken on ReduceOnly assets,
while not force-liquidating users who already hold them.

### KilledByBankruptcy

The bank was killed by a bankruptcy event and is irrecoverable. All operations are blocked. This
state can only be set programmatically by the `handle_bankruptcy` instruction when a bankruptcy
event wipes out all remaining assets in the bank. It **cannot** be set manually by an admin.

## Summary Table

| State | Deposit | Borrow | Withdraw | Repay | Liquidate | Initial Margin | Maintenance Margin |
|-------|---------|--------|----------|-------|-----------|----------------|--------------------|
| **Paused** | No | No | No | No | No | N/A | N/A |
| **Operational** | Yes | Yes | Yes | Yes | Yes | Full value | Full value |
| **ReduceOnly** | No | No | Yes | Yes | Yes | $0 | Full value |
| **KilledByBankruptcy** | No | No | No | No | No | N/A | N/A |
```

**File:** programs/marginfi/src/state/bank.rs (L418-439)
```rust
        if let Some(new_state) = config.operational_state {
            // JupLend banks must be activated exactly once through `juplend_init_position`.
            check!(
                !(self.config.asset_tag == ASSET_TAG_JUPLEND
                    && self.config.operational_state == BankOperationalState::Uninitialized),
                MarginfiError::Unauthorized
            );
            // These states are unreachable by configuration
            check!(
                new_state != BankOperationalState::KilledByBankruptcy
                    && new_state != BankOperationalState::Uninitialized,
                MarginfiError::Unauthorized
            );
            // Log operational state change
            let old_state = self.config.operational_state;
            self.config.operational_state = new_state;
            msg!(
                "Operational state changed from {:?} to {:?}",
                old_state,
                new_state
            );
        }
```

**File:** guides/ADMIN/PERMISSIONS_AND_ROLES.md (L147-159)
```markdown
### Blocked while paused

All normal user flows are disabled:

- Deposit, Borrow, Withdraw, Repay (both native banks and integration banks — Kamino, Drift,
  Juplend, Solend)
- Order placement / order flows
- Account transfer
- Classic liquidation (`LendingAccountLiquidate`)
- Permissionless bank-fee collection
- Permissionless bad-debt settlement (`HandleBankruptcy` when called by a non-admin, even on banks
  with the `PERMISSIONLESS_BAD_DEBT_SETTLEMENT` flag)
- Admin bank configuration changes that route through `LendingPoolConfigureBank`
```

**File:** programs/marginfi/src/state/panic_state.rs (L45-49)
```rust
    fn unpause(&mut self) {
        self.pause_flags &= !Self::FLAG_PAUSED;
        self.pause_start_timestamp = 0;
        self.consecutive_pause_count = 0;
    }
```

**File:** programs/marginfi/src/instructions/marginfi_group/panic_unpause.rs (L7-37)
```rust
pub fn panic_unpause(ctx: Context<PanicUnpause>) -> Result<()> {
    let mut fee_state = ctx.accounts.fee_state.load_mut()?;
    let current_timestamp = Clock::get()?.unix_timestamp;

    require!(
        fee_state.panic_state.is_paused_flag(),
        crate::errors::MarginfiError::ProtocolNotPaused
    );

    fee_state.panic_state.unpause_if_expired(current_timestamp);

    if fee_state.panic_state.is_paused_flag() {
        fee_state.panic_state.unpause();
        msg!(
            "Protocol manually unpaused by admin at timestamp: {}",
            current_timestamp
        );
    } else {
        msg!(
            "Protocol was already auto-unpaused due to expiration at timestamp: {}",
            current_timestamp
        );
    }

    msg!(
        "Consecutive pause count reset to: {}",
        fee_state.panic_state.consecutive_pause_count
    );

    Ok(())
}
```

**File:** tests/specs/emode/e05_panicMode.spec.ts (L304-322)
```typescript
  it("(user 2 aka liquidator) tries to deposit funds - fails due to pause", async () => {
    const user = users[2];
    const userAccount = user.accounts.get(USER_ACCOUNT_E);

    let tx = new Transaction().add(
      await depositIx(user.mrgnBankrunProgram, {
        marginfiAccount: userAccount,
        bank: usdcBank,
        tokenAccount: user.usdcAccount,
        amount: new BN(0.0001 * 10 ** ecosystem.usdcDecimals),
        depositUpToLimit: false,
      })
    );
    tx.recentBlockhash = await getBankrunBlockhash(bankrunContext);
    tx.sign(user.wallet);
    const result = await banksClient.tryProcessTransaction(tx);
    // Protocol paused
    assertBankrunTxFailed(result, 6080);
  });
```
