### Title
Group-level rate limiter can be bypassed within a single atomic transaction because the check is read-only and not updated between calls - ([File: programs/marginfi/src/utils/general.rs])

### Summary
The group-level outflow rate limiter is designed as a circuit breaker that caps aggregate USD outflow from a group per hour/day, explicitly to protect against abused or compromised risk workflows. However, the check performed on every borrow/withdraw is purely read-only against a cached counter on the `MarginfiGroup` account that is *only* mutated later, off-chain, by a delegated admin calling `update_group_rate_limiter`. Because the check never accumulates state within the same transaction, a user can bundle multiple borrow/withdraw instructions (against the same or different banks) into a single atomic transaction, and each instruction independently checks against the same unchanged "remaining capacity" value. This lets a single transaction bypass the configured group rate-limit cap by an arbitrary multiple, defeating the entire purpose of the limiter — directly analogous to the ZetaChain bug where a per-message gas-meter reset let multiple `MsgGasPriceVoter` messages in one transaction each bypass cumulative gas accounting.

### Finding Description
The rate-limit check lives in `record_withdrawal_outflow`: [1](#0-0) 

This checks `group.rate_limiter.hourly/daily.effective_remaining_capacity(...)` — a pure read of the cached `MarginfiGroup` counters — and only emits a `RateLimitFlowEvent` for later off-chain aggregation. It never mutates `group.rate_limiter` itself.

The design intent is explicitly documented: [2](#0-1) 

And the group account is only updated later by the delegate flow admin in `update_group_rate_limiter`, after aggregating events off-chain: [3](#0-2) 

The project's own test suite explicitly confirms and names this behavior — "group rate limiter is read-only during user instructions" — and shows the on-chain counter is unchanged after a borrow/repay: [4](#0-3) 

Because the same-request/same-transaction accounting never accumulates, every instance of `lending_account_borrow`, `lending_account_withdraw` (and the Kamino/Drift/Solend/JupLend withdraw variants, which all call the identical `record_withdrawal_outflow` helper) checks against the identical, un-mutated `remaining_capacity` value within one atomic transaction. This mirrors the ZetaChain root cause exactly: a per-message reset/check ignores the accumulated effect of prior messages in the same atomic unit, letting the attacker multiply an intended cap by bundling repeated actions.

### Impact Explanation
The group-level rate limiter exists specifically as a last-resort defense against abused or compromised components (compromised admin keys, oracle manipulation, bugged risk logic) — the same rationale documented for the sibling deleverage-withdraw limit ("a defense if the risk workflow is abused or compromised"). An unprivileged user can defeat this circuit breaker entirely by splitting what should be one large, capped outflow into N separate borrow/withdraw instructions inside a single transaction (still subject only to individual bank-level rate limits and account health, both of which are per-instruction and don't prevent this). This is a concrete unauthorized bypass of a protocol-level security control meant to bound aggregate capital flight from a group, undermining the very insolvency/bank-run protection the feature was built for.

### Likelihood Explanation
This requires no special privilege — any account holder who can pass the normal per-instruction health/bank-rate-limit checks can construct a single transaction containing multiple withdraw/borrow instructions. The exploit path (multiple instructions in one signed transaction) is standard, low-cost, and always available on Solana; the only requirement is sufficient collateral/liquidity for each individual instruction, which is far less restrictive than the intended aggregate USD cap.

### Recommendation
Track and enforce the group-level rate limit intra-transaction, not just via the stale cached counter. Options:
- Maintain an ephemeral, transaction-scoped accumulator (e.g., via a mutable, transaction-lifetime account or CPI-shared context) that is checked and updated by every outflow instruction within the same transaction, in addition to the read-only cached check.
- Alternatively, make the group account genuinely mutable per outflow instruction (accepting the serialization cost this was designed to avoid), or bound the maximum outflow permitted per single transaction/instruction to a fraction of the configured hourly/daily cap so that no single transaction can materially exceed it regardless of how many instructions it bundles.

### Proof of Concept
1. Admin sets `groupHourly` outflow limit to $10 via `configure_group_rate_limits` (as in the test at `tests/specs/basic/17_rateLimiter.spec.ts:557-564`).
2. A user builds a single transaction containing, e.g., 5 separate `lending_account_borrow` instructions of $9 each (each individually within bank-level limits and account health).
3. Each instruction independently calls `record_withdrawal_outflow`, which reads `group.rate_limiter.hourly.effective_remaining_capacity()` — still $10 for every one of the 5 instructions, because none of them mutate the group account (confirmed read-only behavior at `tests/specs/basic/17_rateLimiter.spec.ts:748-785`).
4. All 5 instructions succeed in the same transaction, producing $45 of USD outflow in one atomic action against a configured $10/hour cap — a 4.5x bypass of the intended limit, with the bypass factor scaling with however many instructions are packed into the transaction.

### Citations

**File:** programs/marginfi/src/utils/general.rs (L483-511)
```rust
        // Group-level rate limiting: read-only validation + event emission.
        // The admin aggregates events off-chain and calls update_group_rate_limiter.
        if group_rate_limit_enabled {
            check!(price > I80F48::ZERO, MarginfiError::InvalidRateLimitPrice);

            let value = calc_value(
                I80F48::from_num(balance_amount),
                price,
                bank.get_balance_decimals(),
                None,
            )?;
            if group.rate_limiter.hourly.is_enabled() {
                let remaining = group
                    .rate_limiter
                    .hourly
                    .effective_remaining_capacity(clock.unix_timestamp);
                if value.to_num::<i64>() > remaining {
                    return Err(MarginfiError::GroupHourlyRateLimitExceeded.into());
                }
            }
            if group.rate_limiter.daily.is_enabled() {
                let remaining = group
                    .rate_limiter
                    .daily
                    .effective_remaining_capacity(clock.unix_timestamp);
                if value.to_num::<i64>() > remaining {
                    return Err(MarginfiError::GroupDailyRateLimitExceeded.into());
                }
            }
```

**File:** guides/ADMIN/RATE_LIMITS_AND_DELEVERAGE_WITHDRAW_LIMITS.md (L39-53)
```markdown
### 2. User transaction path

During a withdraw or borrow:

- The bank rate limiter is updated immediately on the writable bank account.
- The group rate limiter is only checked read-only.
- The protocol converts the flow to USD using the instruction price/oracle path.
- If the projected group hourly or daily capacity is exceeded, the user instruction fails.
- A `RateLimitFlowEvent` is emitted for off-chain aggregation.

Important details:

- Flashloans, liquidations, and deleverages skip the normal rate-limit accounting path.
- `RateLimitFlowEvent` is an indexing aid, not a source of truth. Solana log truncation can drop
  events, so the off-chain worker must reconcile gaps instead of assuming no event means no flow.
```

**File:** programs/marginfi/src/instructions/marginfi_group/update_group_rate_limiter.rs (L19-68)
```rust
pub fn update_group_rate_limiter(
    ctx: Context<UpdateGroupRateLimiter>,
    outflow_usd: Option<u64>,
    inflow_usd: Option<u64>,
    update_seq: u64,
    event_start_slot: u64,
    event_end_slot: u64,
) -> MarginfiResult {
    let mut group = ctx.accounts.marginfi_group.load_mut()?;
    let clock = Clock::get()?;

    check!(
        outflow_usd.is_some() || inflow_usd.is_some(),
        MarginfiError::GroupRateLimiterUpdateEmpty
    );
    validate_event_slots(
        event_start_slot,
        event_end_slot,
        group.rate_limiter_last_admin_update_slot,
    )?;
    check!(
        event_end_slot <= clock.slot,
        MarginfiError::GroupRateLimiterUpdateFutureSlot
    );
    check!(
        clock.slot.saturating_sub(event_end_slot) <= MAX_RATE_LIMIT_UPDATE_LAG_SLOTS,
        MarginfiError::GroupRateLimiterUpdateStale
    );
    check!(
        update_seq == group.rate_limiter_last_admin_update_seq.saturating_add(1),
        MarginfiError::GroupRateLimiterUpdateOutOfOrderSeq
    );

    if let Some(inflow) = inflow_usd {
        check!(
            is_valid_rate_limit_amount(inflow),
            MarginfiError::InvalidConfig
        );
        group
            .rate_limiter
            .record_inflow(inflow, clock.unix_timestamp);
        msg!("Group rate limiter inflow recorded: {} USD", inflow);
    }

    if let Some(outflow) = outflow_usd {
        group
            .rate_limiter
            .try_record_outflow(outflow, clock.unix_timestamp)?;
        msg!("Group rate limiter outflow recorded: {} USD", outflow);
    }
```

**File:** tests/specs/basic/17_rateLimiter.spec.ts (L748-785)
```typescript
  it("(user 2) group rate limiter is read-only during user instructions", async () => {
    // Configure group hourly limit
    await setRateLimits({
      bankHourly: usdcNative(1_000),
      bankDaily: new BN(0),
      groupHourly: new BN(100),
      groupDaily: new BN(0),
    });

    const groupBefore = await program.account.marginfiGroup.fetch(
      marginfiGroup.publicKey,
    );
    const outflowBefore =
      groupBefore.rateLimiter.hourly.curWindowOutflow.toNumber();

    // Borrow 5 USDC - this should succeed (within limit) but NOT update group state
    await borrowUsdc(usdcNative(5));

    const groupAfterBorrow = await program.account.marginfiGroup.fetch(
      marginfiGroup.publicKey,
    );
    // Group rate limiter should NOT have changed (it's read-only during user instructions)
    assertBNEqual(
      groupAfterBorrow.rateLimiter.hourly.curWindowOutflow,
      outflowBefore,
    );

    // Repay 5 USDC - group state should also remain unchanged
    await repayUsdc(usdcNative(5));

    const groupAfterRepay = await program.account.marginfiGroup.fetch(
      marginfiGroup.publicKey,
    );
    assertBNEqual(
      groupAfterRepay.rateLimiter.hourly.curWindowOutflow,
      outflowBefore,
    );
  });
```
