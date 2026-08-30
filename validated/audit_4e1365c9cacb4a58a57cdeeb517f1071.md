### Title
Borrower can act as their own liquidator to offload their own bad debt onto lenders via `socialize-debt-asset` - (File: `mainnet/contracts/market/v0-4-market.clar`)

### Summary
`market.clar`'s `liquidate` function sets `(liquidator contract-caller)` and lets the caller specify the `borrower` argument with no check that `liquidator != borrower`, and the seized collateral defaults to going back to the liquidator (`(actual-receiver (match collateral-receiver recv recv liquidator))`). This mirrors the External Report's core flaw: a party who should be *penalized* by a slashing/settlement mechanism can instead be the one who *triggers and receives* that mechanism's payout, letting them profit from (or escape the cost of) their own default instead of a neutral third party bearing the risk/reward.

### Finding Description
`liquidate` in `v0-4-market.clar` computes the liquidation amounts purely from the position's health (`current-ltv`, `ltv-liq-partial/full`, `liq-penalty-min/max`) and never checks who the caller is relative to the `borrower`: [1](#0-0) [2](#0-1) 

When the caller supplies their own address as `borrower`, they become their own liquidator. The collateral seized (with the liquidator bonus, `calc-liq-collateral-repay`) is sent to `actual-receiver`, which defaults to the liquidator (themselves) unless a different `collateral-receiver` is given: [3](#0-2) 

If the amount of debt the caller chooses to repay is just enough to drain the *entire* remaining collateral balance but not enough to fully retire the debt attributable to it, the position ends up with `coll-removed == 0` remaining and `no-collateral-left` becomes true, triggering `socialize-debt-asset`, which writes off the rest of the borrower's debt at the expense of the lending pool (`vault-socialize-debt`, reducing `lindex` for all depositors): [4](#0-3) [5](#0-4) 

Because the borrower controls both sides of the "trade" (they are both debtor and liquidator), they can time and size the self-liquidation to withdraw the maximum amount of their own collateral (at the liquidator bonus rate, which is normally compensation paid by the loser of the trade to an honest, risk-taking third party) while shifting the un-repaid remainder of their own debt onto the socialized-debt mechanism intended to absorb *unavoidable* insolvency, not deliberate exits. This is analogous to the report's "relayer submits wrong evidence and calls `slash()` without losing funds": the party who is supposed to be penalized instead captures the reward path and pushes the cost onto uninvolved third parties (lenders), rather than a neutral counterparty bearing the risk.

Notably, the codebase explicitly acknowledges and defends against a similar "self-liquidation" class of exploit in `collateral-add` (egroup-manipulation self-liquidation), but no equivalent `liquidator != borrower` guard exists in `liquidate` itself: [6](#0-5) 

### Impact Explanation
This allows a borrower to extract collateral from their own position at the liquidator bonus rate while transferring the unpaid remainder of their debt to the vault's lenders via bad-debt socialization — i.e., theft of user funds (lender deposits) and protocol insolvency exposure, since the lender pool absorbs debt that the borrower engineered to avoid repaying in full. This falls under the Critical impact category (direct theft of user funds at rest / protocol insolvency).

### Likelihood Explanation
Requires the caller's own position to already satisfy the liquidation health-check (`current-ltv >= ltv-liq-partial`), i.e., the position must be genuinely eligible for liquidation (e.g., borrower took on max leverage and a small price move or interest accrual tips them over threshold). Any ordinary borrower can reach this state without needing any privileged role, oracle manipulation, or flashloan — it only needs the `liquidate` entry point's lack of a self-liquidation restriction, making this reachable purely from an ordinary principal's own account.

### Recommendation
Add an explicit check in `liquidate` (and `liquidate-redeem`) that `contract-caller` (the liquidator) is not the `borrower`, i.e., `(asserts! (not (is-eq liquidator borrower)) ERR-SELF-LIQUIDATION)`, or at minimum ensure that the socialize-debt path cannot be triggered when the caller/receiver of seized collateral is the borrower themselves. This eliminates the borrower's ability to be both the party causing the default and the party rewarded for "resolving" it.

### Proof of Concept
1. Alice deposits collateral and borrows near the maximum LTV allowed by her egroup via `borrow`.
2. Price drift or interest accrual pushes her `current-ltv` at/above `ltv-liq-partial` (a normal, unprivileged occurrence — no oracle bug or flashloan needed).
3. Alice calls `liquidate(borrower=alice, collateral-ft, debt-ft, debt-amount, min-collateral-expected, collateral-receiver=none, price-feeds)` from her own account (`contract-caller == alice == borrower`).
4. `liquidate` computes `debt-to-repay`/`coll-final` from health parameters only, with no `liquidator != borrower` check [1](#0-0) ; Alice pays just enough of her own debt to drain her remaining collateral (`coll-final == user-coll-balance`), receiving that collateral back to herself as `actual-receiver` (default liquidator) [3](#0-2) .
5. Since `coll-removed == 0` remains and no other collateral/debt exists, `no-collateral-left` is true, and the residual scaled debt is passed to `socialize-debt-asset`, writing off Alice's remaining debt at the lending pool's expense [4](#0-3) [5](#0-4) .
6. Alice ends up holding her collateral (recovered at the liquidator-bonus discount relative to what she owed) while lenders bear the unpaid remainder — a value transfer she engineered by being both debtor and liquidator.

### Citations

**File:** mainnet/contracts/market/v0-4-market.clar (L1382-1394)
```text
(define-public (liquidate
                (borrower principal)
                (collateral-ft <ft-trait>)
                (debt-ft <ft-trait>)
                (debt-amount uint)
                (min-collateral-expected uint)
                (collateral-receiver (optional principal))
                (price-feeds (optional (list 3 (buff 8192)))))
  (let (
    (feeds-check (try! (write-feeds price-feeds)))
    (liquidator contract-caller)
    (position (try! (get-liquidation-position borrower)))
    (pos-full (try! (get-full-position borrower)))
```

**File:** mainnet/contracts/market/v0-4-market.clar (L1504-1512)
```text
          ;; Collateral receiver defaults to liquidator if not specified
          (actual-receiver (match collateral-receiver recv recv liquidator))
          (coll-removed (try! (contract-call? .v0-market-vault
                              collateral-remove
                              borrower
                              coll-final
                              collateral-ft
                              coll-aid
                              actual-receiver)))
```

**File:** mainnet/contracts/market/v0-4-market.clar (L1533-1560)
```text

      ;; Handle bad debt socialization if no collateral left
      (let ((bad-debt-socialized 
              (if no-collateral-left
                  (let ((stripped-debt-list (filter-out-debt-asset (get debt pos-full) debt-aid))
                        (fresh-debt-list (if (is-eq debt-updated u0)
                                             stripped-debt-list
                                             (unwrap-panic (as-max-len?
                                               (append stripped-debt-list
                                                       { aid: debt-aid, scaled: debt-updated })
                                               u64)))))
                    (if (> (len fresh-debt-list) u0) ;; if still has debt
                      (let ((socialization-result (fold socialize-debt-asset 
                                                        fresh-debt-list 
                                                        { borrower: borrower, success: true })))
                        (asserts! (get success socialization-result) ERR-BAD-DEBT-SOCIALIZATION-FAILED)
                        ;; emit bad-debt-socialized event
                        (print {
                          action: "bad-debt-socialized",
                          caller: contract-caller,
                          data: {
                            borrower: borrower,
                            debt-list: fresh-debt-list
                          }
                        })
                        true)
                      false))
                  false)))
```

**File:** mainnet/contracts/vault/v0-vault-sbtc.clar (L961-982)
```text
    (var-set lindex new-lindex)
    (var-set principal-scaled (if (> scaled-principal scaled-amount) (- scaled-principal scaled-amount) u0))
    (var-set total-borrowed (if (> borrowed principal-reduction) (- borrowed principal-reduction) u0))
    (var-set assets (if (> current-assets principal-reduction) (- current-assets principal-reduction) u0))

    (print {
      action: "socialize-debt",
      caller: contract-caller,
      data: {
        scaled-amount: scaled-amount,
        debt-reduction: debt-reduction,
        principal-reduction: principal-reduction,
        old-lindex: current-lindex,
        new-lindex: new-lindex,
        old-total-assets: old-total-assets,
        principal-scaled: (if (> scaled-principal scaled-amount) (- scaled-principal scaled-amount) u0),
        total-borrowed: (if (> borrowed principal-reduction) (- borrowed principal-reduction) u0),
        index: idx
      }
    })

    (ok true)))
```

**File:** docs/High-Level-Overview.md (L103-106)
```markdown
**Attack Scenarios Prevented**:
- Dust collateral poisoning (adding tiny amounts to worsen position)
- Self-liquidation exploits (manipulating egroup to trigger liquidation with penalty)
- Accidental position deterioration (users can't make themselves liquidatable by adding collateral)
```
