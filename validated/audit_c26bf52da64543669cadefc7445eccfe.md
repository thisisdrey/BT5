No vulnerability found for this question.

The `getActualSupply` vs `totalSupply` bug class is specific to Balancer's pre-minted BPT design, where the pool token's `totalSupply()` overstates supply because unminted/reserved BPT sit at the vault and aren't burned. Zest's vaults use a native SIP-010 fungible token (`ft-get-supply zft`) for shares, which has no such pre-minting quirk — `total-supply` always reflects genuinely circulating shares [1](#0-0) .

Critically, both `deposit` and `redeem` call `(try! (accrue))` as the very first binding in their `let`, which synchronously mints any pending treasury-fee shares to `dao-treasury` before the subsequent `convert-to-shares-preview`/`convert-to-assets-preview` calls read `total-supply` [2](#0-1) . Because Clarity `let` bindings evaluate sequentially with side effects taking place immediately, by the time shares are calculated the supply figure already includes the newly-minted treasury LP shares — there is no stale/understated supply value analogous to Balancer's un-minted BPT gap [3](#0-2) .

The `calc-treasury-lp-preview`/`total-supply-preview` helpers exist precisely to account for this pending mint in read-only preview contexts, and the accrual logic mints treasury shares using `total-supply` (not a preview) at the moment the debt delta is realized, so it can't be manipulated by an outside actor supplying stale data [4](#0-3) . No Balancer-style pool, join/exact-tokens-in slippage computation, or external LP `totalSupply()` dependency exists anywhere in the in-scope `mainnet/contracts/**` tree, so there is no reachable analog to the reported bug class.

### Citations

**File:** mainnet/contracts/vault/v0-vault-stx.clar (L287-287)
```text
(define-private (total-supply) (ft-get-supply zft))
```

**File:** mainnet/contracts/vault/v0-vault-stx.clar (L348-368)
```text
;; -- Treasury LP preview helpers --------------------------------------------

(define-private (calc-treasury-lp-preview)
  (let ((scaled-principal (var-get principal-scaled))
        (idx (var-get index))
        (next (next-index))
        (old-debt (mul-div-down scaled-principal idx INDEX-PRECISION))
        (new-debt (mul-div-down scaled-principal next INDEX-PRECISION))
        (debt-delta (if (> new-debt old-debt) (- new-debt old-debt) u0))
        (reserve-inc (mul-div-down debt-delta (var-get fee-reserve) BPS))
        (ta-preview (total-assets-preview)))
    (if (> reserve-inc u0)
        (mul-div-down reserve-inc (total-supply) (- ta-preview reserve-inc))
        u0)))

(define-private (total-supply-preview)
  (let ((current-supply (total-supply))
        (treasury-lp (calc-treasury-lp-preview)))
    (+ current-supply treasury-lp)))

(define-private (utilization)
```

**File:** mainnet/contracts/vault/v0-vault-stx.clar (L763-806)
```text
(define-public (deposit (amount uint) (min-out uint) (recipient principal))
    (let (
      (states (var-get pause-states))
      (u (try! (accrue)))
      (account contract-caller)
      (CAP-SUPPLY (var-get cap-supply))
      (current-assets (var-get assets))
      (inkind (convert-to-shares-preview amount)))

    (asserts! (not (get deposit states)) ERR-PAUSED)
    (asserts! (var-get initialized) ERR-INIT)
    (asserts! (not (var-get in-flashloan)) ERR-REENTRANCY)
    (asserts! (> amount u0) ERR-AMOUNT-ZERO)
    (asserts! (>= inkind min-out) ERR-SLIPPAGE)
    (asserts! (<= (+ current-assets amount) CAP-SUPPLY) ERR-SUPPLY-CAP-EXCEEDED)

    (try! (receive-underlying amount account))
    (try! (ft-mint? zft inkind recipient))
    (var-set assets (+ current-assets amount))
    
    (print {
      action: "deposit",
      caller: contract-caller,
      data: {
        depositor: account,
        recipient: recipient,
        amount: amount,
        shares-minted: inkind,
        assets: (+ current-assets amount)
      }
    })

    (ok inkind)))

(define-public (redeem (amount uint) (min-out uint) (recipient principal))
  (let (
    (states (var-get pause-states))
    (u (try! (accrue)))
    (account contract-caller)
    (current-assets (var-get assets))
    (balance (get-balance-internal account))
    (balance-check (asserts! (>= balance amount) ERR-INSUFFICIENT-BALANCE))
    (available-assets (get-available-assets))
    (inkind (convert-to-assets-preview amount)))
```

**File:** mainnet/contracts/vault/v0-vault-stx.clar (L835-863)
```text
(define-public (accrue)
  (let ((states (var-get pause-states))
        (idx (var-get index))
        (lidx (var-get lindex)))
      (if (get accrue states)
          ;; PAUSED: Pass-through without reverting
          (ok { index: idx, lindex: lidx })
          ;; NOT PAUSED: Normal accrual logic
          (let ((next (next-index))
                (nliq (next-liquidity-index))
                (scaled-principal (var-get principal-scaled))
                (old-debt (mul-div-down scaled-principal idx INDEX-PRECISION))
                (new-debt (mul-div-down scaled-principal next INDEX-PRECISION))
                (debt-delta (if (> new-debt old-debt) (- new-debt old-debt) u0))
                (reserve-inc (mul-div-down debt-delta (var-get fee-reserve) BPS))
                (treasury-lp (if (> reserve-inc u0) (mul-div-down reserve-inc (total-supply) (- (total-assets-preview) reserve-inc)) u0)))
            (if (not (is-eq idx next))
                (var-set index next)
                false)
            (if (not (is-eq lidx nliq))
                (var-set lindex nliq)
                false)
            (if (> treasury-lp u0)
                (try! (ft-mint? zft treasury-lp .dao-treasury))
                false)
            (if (or (not (is-eq idx next)) (not (is-eq lidx nliq)))
                (var-set last-update stacks-block-time)
                false)
            (ok { index: next, lindex: nliq })))))
```
