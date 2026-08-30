### Title
Vault `accrue` treasury-fee mint math can underflow and permanently brick deposit/redeem/borrow/repay/transfer - (File: `mainnet/contracts/vault/v0-vault-usdc.clar`)

### Summary
Every core vault entry point (`deposit`, `redeem`, `transfer`, and — via `market.clar`'s `accrue-and-cache`/`vault-accrue` — `borrow`, `repay`, `liquidate`, `socialize-debt`) begins by unconditionally calling `(try! (accrue))`. `accrue` computes a treasury fee-share mint using an unchecked, unsigned subtraction. If that subtraction underflows, the Clarity runtime aborts the whole transaction (not a recoverable `err` that `try!` can route around), which means a single bad internal computation inside `accrue` — analogous to `RewardsSource.collectRewards` reverting and blocking `OgvStaking.stake/unstake/extend` — silently disables every user-facing function of the vault.

### Finding Description
`accrue`, present identically in `v0-vault-stx.clar`, `v0-vault-sbtc.clar`, `v0-vault-ststx.clar`, `v0-vault-ststxbtc.clar`, `v0-vault-usdc.clar`, and `v0-vault-usdh.clar`, computes the DAO treasury's fee share as: [1](#0-0) 

```
(reserve-inc (mul-div-down debt-delta (var-get fee-reserve) BPS))
(treasury-lp (if (> reserve-inc u0)
                 (mul-div-down reserve-inc (total-supply) (- (total-assets-preview) reserve-inc))
                 u0))
...
(if (> treasury-lp u0) (try! (ft-mint? zft treasury-lp .dao-treasury)) false)
```

The subtraction `(- (total-assets-preview) reserve-inc)` is unsigned. In Clarity, unsigned subtraction that would go negative is a runtime error that aborts the entire transaction outright — it is not an `(err ...)` value, so it cannot be intercepted by `try!`/`unwrap!` anywhere in the call chain. Whenever the freshly-accrued protocol reserve fee (`reserve-inc`) is greater than or equal to `total-assets-preview` (the vault's asset base), `accrue` traps and the transaction reverts unconditionally.

`accrue` is not an optional/best-effort call — it is the first thing every state-changing vault function does: [2](#0-1) 

`deposit`, `redeem`, and `transfer` all call `(try! (accrue))` before doing anything else, and the market router forces the same coupling for `borrow`/`repay`/liquidation flows through `accrue-and-cache` → `vault-accrue` → `accrue`: [3](#0-2) [4](#0-3) 

This is the exact same coupling pattern described in the external report: a peripheral bookkeeping mechanism (`RewardsSource.collectRewards` / here, the treasury fee-share mint inside `accrue`) is force-called from every core user path, so any failure mode in that peripheral logic escalates into a complete denial of the core functionality (staking there, deposit/redeem/borrow/repay here).

A condition where `reserve-inc >= total-assets-preview` is plausible without any DAO misconfiguration: it depends only on ordinary usage over time — `debt-delta` and `fee-reserve` (an ordinary DAO-set BPS fee, not a "malicious" or "compromised" configuration) determine `reserve-inc`, while `total-assets-preview` (which nets out already-borrowed liquidity and any pending write-downs) can shrink independently, e.g. after heavy `socialize-debt` write-downs or when most liquidity is out on loan. Once triggered, the vault self-loops: even the DAO's `set-pause-states` path calls `accrue` first when toggling the accrual pause, so the very knob meant to fix the situation also reverts.

### Impact Explanation
Once the underflow condition is hit, `deposit`, `redeem`, `transfer`, `borrow`, and `repay` for that vault all revert unconditionally, because they all route through `accrue`. This is a protocol-level denial of service that freezes user funds already deposited in that vault (they can no longer be redeemed) and prevents any new interaction, including repaying debt to restore health — this lands on "temporary/permanent freezing of funds" (High/Critical depending on duration and whether the DAO can restore state without also calling `accrue`).

### Likelihood Explanation
The trigger condition is reachable purely through ordinary lending/borrowing activity and the standard, intended `fee-reserve` parameter — it does not require any privileged action, oracle manipulation, or DAO compromise. It requires the vault's `total-assets-preview` to fall to/below the freshly accrued reserve increment in a single accrual step, which is more likely in low-liquidity vaults or after large `socialize-debt` write-downs concentrate remaining assets while debt (and thus `debt-delta`) is comparatively large.

### Recommendation
Guard the treasury-fee computation so it degrades gracefully instead of trapping the whole transaction: explicitly check `(<= reserve-inc (total-assets-preview))` before performing the subtraction, and skip/cap the treasury mint (emitting an event) when the invariant doesn't hold, mirroring the report's suggestion to decouple secondary bookkeeping (rewards there, treasury-fee accounting here) from primary state transitions (staking there, deposit/redeem/borrow/repay here) via defensive handling rather than an unconditional dependency.

### Proof of Concept
1. Let a vault (e.g. `v0-vault-usdc`) accrue outstanding debt normally so `fee-reserve` produces a non-zero `reserve-inc` on the next `accrue` call.
2. Drive `total-assets-preview` down relative to the pending `reserve-inc`, e.g. via a sequence of `redeem` calls and/or `socialize-debt` write-downs that shrink `assets`/`total-assets-preview` faster than debt/fees decrease, until `reserve-inc >= total-assets-preview`.
3. Call any state-changing vault function (`deposit`, `redeem`, `transfer`, or via `market.clar`, `borrow`/`repay`). Each internally calls `accrue`, which executes `(- (total-assets-preview) reserve-inc)`; because the subtraction underflows, Clarity aborts the transaction.
4. Every subsequent call to `deposit`, `redeem`, `transfer`, `borrow`, `repay` on that vault now reverts identically, since `accrue` is always invoked first and always hits the same underflow — funds already supplied to the vault become unredeemable and the vault is unusable until the underlying `total-assets-preview`/`reserve-inc` relationship changes (which itself may require operations that are now blocked).

### Citations

**File:** mainnet/contracts/vault/v0-vault-usdc.clar (L754-798)
```text
    (asserts! (not (is-eq current-contract to)) ERR-TOKENIZED-VAULT-PRECONDITIONS)
    (try! (ft-transfer? zft amount from to))
    (match memo to-print (print to-print) 0x)
    (ok true)))

;; -- Vault operations -------------------------------------------------------

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
```

**File:** mainnet/contracts/vault/v0-vault-usdc.clar (L837-862)
```text
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

**File:** mainnet/contracts/market/v0-4-market.clar (L245-268)
```text
(define-private (accrue-and-cache (aid uint))
  (let ((cache-key { timestamp: stacks-block-time, aid: aid })
        (cached? (map-get? index-cache cache-key)))

    (match cached?
      ;; cache HIT: return cached value (1 read only)
      cached-indexes (ok cached-indexes)

      ;; cache MISS: accrue and cache (vault-accrue now returns indexes)
      (let ((indexes (try! (vault-accrue aid))))
        ;; store in cache
        (map-set index-cache cache-key indexes)
        (ok indexes)))))

(define-private (accrue-user-debts (debt-list (list 64 { aid: uint, scaled: uint})))
  (fold accrue-debt-asset debt-list { success: true }))

(define-private (accrue-debt-asset
  (debt-entry { aid: uint, scaled: uint })
  (acc { success: bool }))
  (begin
    ;; this will use cache if available, accrue if not
    (unwrap-panic (accrue-and-cache (get aid debt-entry)))
    acc))
```

**File:** mainnet/contracts/market/v0-4-market.clar (L1320-1330)
```text
        ;; defaults to payer (contract-caller) if not specified
        (account (match on-behalf-of behalf behalf contract-caller))
        
        ;; Step 1: Get position WITHOUT resolving prices
        (position (try! (get-position account)))
        (mask (get mask position))
        
        ;; Step 2: Accrue user's positions (populates cache for ztokens)
        (u-debt (accrue-user-debts (get debt position)))
        
        (borrow-index (get index (unwrap-panic (get-cached-indexes asset-id))))
```
