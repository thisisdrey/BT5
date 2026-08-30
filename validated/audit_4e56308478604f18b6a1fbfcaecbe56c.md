### Title
Absence of a minimum-debt floor in `borrow` allows creation of dust positions that are uneconomical to liquidate - (File: `mainnet/contracts/market/v0-4-market.clar`)

### Summary
Sentiment's M-22 root cause is that a lending market lets positions be opened/kept with debt so small that liquidators have no incentive to liquidate them, causing bad debt to accumulate. Zest's `borrow` entrypoint has the analogous gap: it enforces only `amount > 0` and a collateral-value-based health check, with no minimum notional-debt (or minimum notional-collateral) floor at all.

### Finding Description
`borrow` in `mainnet/contracts/market/v0-4-market.clar` validates: [1](#0-0) 
- `amount > 0`
- asset not borrow-disabled
- pre/post health check via `is-healthy` / `is-healthy-with-mask` [2](#0-1) 

There is no check anywhere in `borrow`, `collateral-add`, or the egroup risk parameters (`docs/egroups.md`) that enforces a minimum USD-notional debt size for a position. The egroup structure only encodes LTV thresholds (`LTV-BORROW`, `LTV-LIQ-PARTIAL`, `LTV-LIQ-FULL`) and penalty bounds, confirmed by the risk-parameter table: [3](#0-2) 

Liquidation itself is purely LTV-driven and pays the liquidator a penalty proportional to the debt being repaid: [4](#0-3) 

This is confirmed empirically by the project's own test suite, which documents that borrowing an amount as small as `1n` (dust) against real collateral succeeds: [5](#0-4) 

Because liquidation payouts scale with the absolute debt size (`calc-liq-collateral-repay`, `calc-liq-debt-repay`), a dust position's liquidation bonus can be smaller than the on-chain transaction cost of performing the liquidation. Unlike Sentiment (where `minDebt`/`minBorrow` are admin-configurable knobs that could be *set* to zero), Zest never implements such a floor in the first place — the vulnerable condition is the protocol's baseline behavior, not a misconfiguration requiring a privileged actor's mistake. Any ordinary user can open (and any market participant can inadvertently leave, via partial repay) a dust debt position that liquidators will rationally ignore, exactly matching the Sentiment bug class.

### Impact Explanation
Dust positions that are uneconomical to liquidate accumulate as unliquidated risk. Once price movement or interest accrual pushes many such small positions underwater simultaneously (or a single position's collateral value decays to near zero while debt remains outstanding), the debt goes unrecovered and the corresponding vault suffers a shortfall that is ultimately covered by depositors' principal (protocol insolvency / permanent freezing of depositor funds for the affected vault), matching the in-scope Critical impact class of protocol insolvency / theft of funds at rest via bad-debt socialization.

### Likelihood Explanation
Likelihood is elevated because no privileged action is required — a normal user simply needs to borrow (or reduce debt down to) a dust amount, which the contract explicitly permits per its own test coverage. This can happen accidentally (partial repayments leaving small residual scaled debt) or be created deliberately by an attacker seeding many dust positions across a low-value asset market to force the protocol/keepers to either eat gas costs disproportionate to recovered value or let bad debt accrue.

### Recommendation
Add a minimum notional-debt (and/or minimum notional-collateral) check in `borrow` (and ideally in `repay`/liquidation-completion paths that could leave a position with tiny residual debt), expressed in USD terms via the existing oracle/notional evaluation pipeline (`get-asset-value`, `get-notional-evaluation`), rejecting borrows or partial repayments that would leave debt below a DAO-configurable floor. Alternatively, require full closure (or a "close entire position" liquidation path with no minimum) for positions falling under the floor so liquidators are always compensated adequately relative to gas costs.

### Proof of Concept
1. Attacker (or any user) calls `market.collateral-add` with a normal amount of collateral for asset A.
2. Attacker calls `market.borrow` for asset B with `amount = 1` (or any dust amount) — this succeeds since the only checks are `amount > 0`, borrow-not-disabled, and the health check, which trivially passes for a dust debt increase against non-trivial collateral: [6](#0-5) 
3. The position now carries dust debt. When the position's LTV eventually crosses `LTV-LIQ-PARTIAL` (e.g., due to price decline or interest accrual), the computed liquidation bonus (`calc-liq-collateral-repay`) on the dust debt is smaller than the liquidator's transaction cost, so no rational liquidator calls `liquidate`.
4. Debt continues to accrue interest unchecked while remaining below any economic liquidation threshold, and is never recovered — repeated across many dust positions this becomes systemic bad debt absorbed by vault depositors.

### Citations

**File:** mainnet/contracts/market/v0-4-market.clar (L726-734)
```text
;; Calculate collateral to seize (includes liquidator bonus)
;; collateral-repay = debt-repay * (BPS + liq-penalty) / BPS
(define-private (calc-liq-collateral-repay (debt-repay uint) (liq-penalty uint)) 
  (mul-bps-down debt-repay (+ BPS liq-penalty)))

;; Calculate actual debt repayment when collateral is capped
;; debt-repay-real = (collateral-amount-usd * BPS) / (BPS + liq-penalty)
(define-private (calc-liq-debt-repay-real (collateral-amount-usd uint) (liq-penalty uint)) 
  (div-bps-down collateral-amount-usd (+ BPS liq-penalty)))
```

**File:** mainnet/contracts/market/v0-4-market.clar (L1269-1287)
```text
    ;; preconditions
    (asserts! (> amount u0) ERR-AMOUNT-ZERO)
    (asserts! (get debt asset) ERR-BORROW-DISABLED)
    (asserts! (is-healthy collateral-value debt-value current-ltvb) ERR-UNHEALTHY)

    ;; Calculate FUTURE debt (after adding this debt)
    ;; For debt: bit position = asset-id + 64 (DEBT-OFFSET)
    (let ((future-mask (bit-or mask (pow u2 (+ asset-id DEBT-OFFSET))))
          (future-group (try! (get-egroup future-mask)))
          ;; Per-egroup borrow disable check (uses FUTURE egroup, not current)
          ;; Each bit in BORROW-DISABLED-MASK corresponds to a debt asset ID (NOT offset by 64)
          (disabled-borrow-mask (get BORROW-DISABLED-MASK future-group))
          (debt-increase (try! (get-asset-value asset amount true)))
          (debt-post-increased (+ debt-value debt-increase)))

    ;; Check if this specific asset is disabled for borrowing in the FUTURE egroup
    (asserts! (is-eq (bit-and disabled-borrow-mask (pow u2 asset-id)) u0) ERR-EGROUP-ASSET-BORROW-DISABLED)
    ;; postconditions
    (asserts! (try! (is-healthy-with-mask collateral-value debt-post-increased future-mask)) ERR-UNHEALTHY)
```

**File:** docs/egroups.md (L16-41)
```markdown
### Risk parameter structure:

```
(
  MASK                 : uint,      // Which assets this applies to (bitmask)
  BORROW-DISABLED-MASK : uint.      // Which borrow assets are disabled in this group (security control)
  LTV-BORROW           : (buff 2),  // Max LTV for borrowing (bps, e.g., 7500 = 75%)
  LTV-LIQ-PARTIAL      : (buff 2),  // LTV threshold for partial liquidation (bps)
  LTV-LIQ-FULL         : (buff 2),  // LTV threshold for full liquidation (bps)
  LIQ-PENALTY-MIN      : (buff 2),  // Min liquidation penalty/bonus (bps)
  LIQ-PENALTY-MAX      : (buff 2),  // Max liquidation penalty/bonus (bps)
  LIQ-CURVE-EXP        : (buff 2)   // Curve exponent for graduated liquidation (bps)
)
```

**Parameter Details:**

| Parameter | Range | Example | Description |
|-----------|-------|---------|-------------|
| `BORROW-DISABLED-MASK` | uint bitmask | u4 (disable sBTC) | Bitmask of debt assets disabled for borrowing in this egroup. Bit N = asset ID N disabled. |
| `LTV-BORROW` | 0-10000 bps | 7500 (75%) | Maximum LTV for new borrows |
| `LTV-LIQ-PARTIAL` | 0-10000 bps | 8500 (85%) | LTV at which partial liquidation starts |
| `LTV-LIQ-FULL` | 0-10000 bps | 9500 (95%) | LTV at which full liquidation allowed |
| `LIQ-PENALTY-MIN` | 0-10000 bps | 100 (1%) | Minimum liquidation penalty |
| `LIQ-PENALTY-MAX` | 0-10000 bps | 1000 (10%) | Maximum liquidation penalty |
| `LIQ-CURVE-EXP` | bps | 10000 (1.0) | Exponent for graduated penalty curve |
```

**File:** local-testing/tests/security/edge-cases.test.ts (L212-223)
```typescript
    it('should reject borrow that results in dust debt', async () => {
      txOk(market.collateralAdd(sbtcToken.identifier, 100000000n, null), alice);

      // Try to borrow very small amount
      const result = txOk(
        market.borrow(usdcToken.identifier, 1n, null, null),
        alice
      );

      // Should succeed but with minimal debt
      expect(result).toBeDefined();
    });
```
