#### Title
Group-level rate limit can be bypassed within a single transaction because the counter is only checked read-only and not incremented between successive withdraw/borrow instructions - ([File: programs/marginfi/src/utils/general.rs])

#### Summary
The reported Alchemy issue is a class of bug where a validation check reads "current state" that can be silently changed by another action before the real state-changing operation completes, letting the check be satisfied repeatedly against a stale snapshot. The closest reachable analog in marginfi-v2 is the group-level rate limiter used by `lending_account_withdraw`/`lending_account_borrow` and the integration withdraw instructions (Kamino/Drift/JupLend/Solend). This limiter is deliberately checked read-only per instruction and is only mutated later, out-of-band, by an admin/`delegate_flow_admin` call to `update_group_rate_limiter`.

#### Finding Description
`record_withdrawal_outflow` validates outflow against `group.rate_limiter` in read-only mode and never mutates the in-account counter during the user instruction: [1](#0-0) 

The design is explicitly documented as "checked read-only during user actions, then settled later from aggregated events," and a spec test confirms the group counter is unchanged immediately after a borrow/repay within the limit: [2](#0-1) [3](#0-2) 

Because the check re-reads `group.rate_limiter.hourly/daily.effective_remaining_capacity()` from the same stale on-chain value for every instruction, and does not decrement it within the same transaction, a single transaction can pack N consecutive `lending_account_withdraw`/`lending_account_borrow` (or per-integration withdraw) instructions, each individually within the configured group limit, and each independently pass the check against the same un-updated "remaining capacity" figure. This is structurally the same "state that may change later (or hasn't yet been recorded)" flaw described in the report: the check assumes no other outflow has happened since it read the counter, but multiple outflows in the same atomic transaction (or across transactions before the admin's periodic aggregation) are not reflected before the next check runs.

#### Impact Explanation
This allows a user (or coordinated users) to exceed the group's configured hourly/daily USD outflow cap by an arbitrary multiple within one transaction, defeating the protocol-wide circuit breaker that group rate limits are meant to provide. However, it does not cause insolvency, bad debt, or unauthorized transfer of another user's funds directly — bank-level rate limits (which are updated inline/immediately on the writable bank account) and the risk engine health checks are unaffected by this gap, so accounts still cannot withdraw more than their own collateral entitles them to. The group limiter is explicitly a secondary, defense-in-depth/monitoring safeguard, not the primary solvency control.

#### Likelihood Explanation
Likelihood is low-to-moderate. Exploiting this requires the group admin to have configured group-level rate limits (an opt-in feature) and requires the attacker to have enough collateral/liquidity to place multiple qualifying withdraw/borrow instructions in one transaction — each such instruction is still fully constrained by bank-level rate limits and account health checks. This is also called out in project documentation as accepted, intentional behavior of an off-chain-reconciled limiter (not a hidden bug), which weakens the case that it's an unintended vulnerability rather than a documented design trade-off.

#### Recommendation
If the group rate limit is intended to be a hard, per-transaction enforceable cap rather than a purely advisory/reconciled one, consider tracking a transaction-local (or slot-local) provisional outflow accumulator that is checked and updated atomically across all outflow instructions within the same transaction, in addition to the async admin reconciliation. Alternatively, explicitly document in `RATE_LIMITS_AND_DELEVERAGE_WITHDRAW_LIMITS.md` that the group limiter cannot prevent multiple same-transaction outflows from collectively exceeding the configured cap, so integrators do not rely on it as a hard security boundary.

#### Proof of Concept
1. Group admin sets `groupHourly` rate limit to, e.g., $10 via `configure_group_rate_limits`.
2. An attacker with sufficient collateral constructs a single transaction containing multiple `lending_account_borrow` (or `lending_account_withdraw`) instructions against different banks, each borrowing $9 worth of value (under the $10 cap).
3. Each instruction independently calls `record_withdrawal_outflow`, which reads `group.rate_limiter.hourly.effective_remaining_capacity()` — still $10 because no instruction in this transaction mutates the group account — so every instruction passes the check.
4. The transaction succeeds with a cumulative outflow far exceeding the configured $10 group hourly limit, as demonstrated by the existing test that confirms the group counter is unchanged after a within-limit borrow/repay in the same transaction: [3](#0-2) .

**Note on confidence:** This is presented as the closest structural analog to the reported bug class (validation based on state that hasn't yet been updated/committed), but its severity is inherently limited because it is a secondary/monitoring control by explicit design, not a primary solvency safeguard, and the documentation already discloses that the group limiter is read-only and reconciled asynchronously. If the project's threat model does not consider the group rate limiter a hard security boundary, this finding may not qualify as a true vulnerability under this program's scope.

### Citations

**File:** programs/marginfi/src/utils/general.rs (L483-522)
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

            emit!(RateLimitFlowEvent {
                group: group_key,
                bank: bank_key,
                mint: bank.mint,
                flow_direction: 0, // outflow
                native_amount,
                mint_decimals: bank.mint_decimals,
                current_timestamp: clock.unix_timestamp,
            });
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

**File:** tests/specs/basic/17_rateLimiter.spec.ts (L748-784)
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
```
