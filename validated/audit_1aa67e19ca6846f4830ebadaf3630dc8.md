### Title
Deposits during 100% vault utilization can mint zero shares, causing depositors to lose their entire deposit - ([File: mainnet/contracts/vault/v0-vault-usdc.clar])

### Summary
The Zest vault contracts (e.g. `v0-vault-usdc.clar`, and equivalently `v0-vault-sbtc.clar`, `v0-vault-ststx.clar`, `v0-vault-ststxbtc.clar`, `v0-vault-usdh.clar`, `v0-vault-stx.clar`) price shares using `total-assets-preview`, which only counts idle vault balance plus *unaccrued interest above already-borrowed principal* — it does not count outstanding borrowed principal as part of vault value. When utilization is 100% (all liquidity lent out) and no interest has yet accrued in the current block, `total-assets-preview` evaluates to `u0` while `total-supply-preview` (outstanding shares) is non-zero. `convert-to-shares-preview` explicitly returns `u0` shares for any deposit amount in this state, and `deposit` has no check preventing a zero-share mint, so a depositor's underlying tokens are pulled in and distributed to existing shareholders while the depositor receives nothing.

### Finding Description
`total-assets`/`total-assets-preview` is computed as: [1](#0-0) 

i.e. `current-assets + max(debt - total-borrowed, 0)`. Outstanding principal that has been lent out via `system-borrow` is *not* added back — only the interest accrued above the originally borrowed principal is added. `system-borrow` moves `amount` out of `current-assets` and into `total-borrowed`, leaving `total-assets` unaffected by the loan itself: [2](#0-1) 

If a borrow consumes all idle liquidity (`current-assets` becomes `u0`) and no time has elapsed since `last-update` (so `next-index == index`, i.e. no interest has accrued yet), `debt == total-borrowed`, so `interest == u0`, and `total-assets-preview` evaluates to `u0` even though `total-supply` (shares) remains non-zero.

`convert-to-shares-preview` handles the `ta == u0, ts != u0` case by returning `u0` shares instead of reverting or handling this state safely: [3](#0-2) 

`deposit` calls `accrue` first (which does not change anything if `time-delta == u0`), computes `inkind` via this preview function, but only asserts `inkind >= min-out` and `amount > u0` — there is no `(> inkind u0)` guard analogous to the one present in `redeem` (`ERR-OUTPUT-ZERO`): [4](#0-3) 

Note `redeem` does have this exact protection (`ERR-OUTPUT-ZERO`), which is asymmetrically missing from `deposit`: [5](#0-4) 

This is structurally the same class of bug as the referenced Inverter `FM_Rebasing_v1` finding: the share-price/index update (`accrue`/`_rebase`) does not correct a degenerate zero-denominator state before mint, so a depositor entering during that window is priced incorrectly and other existing holders benefit at the depositor's expense — except here the loss to the depositor is total (100%) rather than partial.

### Impact Explanation
A depositor's entire deposited amount is absorbed into `current-assets` (raising `total-assets` for all existing shareholders) while the depositor receives `0` `zft` shares. This is a direct, irreversible loss of the depositor's principal for the direct benefit of other users — this qualifies as **Critical: direct theft of user funds at rest**, since the funds are transferred into the vault via `receive-underlying` but no compensating claim (shares) is minted to the depositor.

### Likelihood Explanation
This can occur non-maliciously any time vault utilization reaches 100% (e.g. cap-debt near-fully drawn by `system-borrow`, or a large borrow draining `current-assets` to zero) and a deposit lands in the same block/timestamp as that borrow (`time-delta == 0`, so `next-index == index` and no interest has yet accrued). It can also be deliberately engineered by an attacker who front-runs/co-locates a borrow that drains `current-assets` immediately before a victim's deposit, or is a griefing vector where the attacker (or a colluding borrower) intentionally creates the zero-`total-assets` window and then deposits themselves to see the effect, or targets another depositor's pending transaction. Because `deposit` places no lower bound on `inkind`, a caller who passes `min-out = 0` (the most naive/default usage, since they have no reason to expect the exchange rate to be worse than 1:1 at deposit-time) will not be protected by slippage checks.

### Recommendation
- Add `(asserts! (> inkind u0) ERR-OUTPUT-ZERO)` to `deposit`, mirroring the existing check in `redeem`, so any deposit that would mint zero shares reverts instead of silently consuming the depositor's funds.
- More fundamentally, reconsider whether `total-assets`/`total-assets-preview` should include outstanding `total-borrowed` principal (not just interest above it) so that the vault's asset accounting reflects true total value locked (idle + interest + principal owed), preventing `total-assets` from artificially collapsing to zero purely because liquidity is temporarily fully utilized.

### Proof of Concept
1. Vault starts empty. Depositor A deposits `1000` USDC → `total-supply-preview() == 0` branch returns `amount`, A receives `1000` shares; `assets = 1000`.
2. An authorized borrower contract calls `system-borrow(1000, receiver)` at the same `stacks-block-time` as the last accrue update: `current-assets -> 0`, `total-borrowed -> 1000`, `principal-scaled` set so `total-debt() == 1000` at the unchanged `index`. [6](#0-5) 
3. In the same timestamp, Depositor B calls `deposit(500, 0, B)`. `accrue()` sees `time-delta == 0`, so `next-index == index`, and `total-assets-preview()` computes `debt(1000) - borrowed(1000) = 0` interest, plus `current-assets(0)` → `total-assets-preview() == 0`. [7](#0-6) 
4. `convert-to-shares-preview(500)` hits the `ta == u0` branch and returns `u0` shares. [3](#0-2) 
5. `deposit` asserts `inkind(0) >= min-out(0)` — passes. `receive-underlying(500, B)` pulls B's 500 USDC into the vault; `ft-mint? zft 0 B` mints B zero shares; `assets` becomes `500`. [8](#0-7) 
6. Result: B has transferred 500 USDC into the vault and holds `0` shares — a full loss of B's deposit, with the value now backing A's (and other pre-existing holders') shares instead.

### Citations

**File:** mainnet/contracts/vault/v0-vault-usdc.clar (L306-313)
```text
(define-private (convert-to-shares-preview (amount uint))
  (let ((ta (total-assets-preview))
        (ts (total-supply-preview)))
    (if (is-eq ts u0)
        amount
        (if (is-eq ta u0)
            u0
            (mul-div-down amount ts ta)))))
```

**File:** mainnet/contracts/vault/v0-vault-usdc.clar (L332-344)
```text
(define-private (total-assets)
  (let ((current-assets (var-get assets))
        (debt (total-debt))
        (borrowed (var-get total-borrowed))
        (interest (if (> debt borrowed) (- debt borrowed) u0)))
    (+ current-assets interest)))

(define-private (total-assets-preview)
  (let ((current-assets (var-get assets))
        (debt (debt-preview))
        (borrowed (var-get total-borrowed))
        (interest (if (> debt borrowed) (- debt borrowed) u0)))
    (+ current-assets interest)))
```

**File:** mainnet/contracts/vault/v0-vault-usdc.clar (L765-783)
```text
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
```

**File:** mainnet/contracts/vault/v0-vault-usdc.clar (L799-815)
```text
    (account contract-caller)
    (current-assets (var-get assets))
    (balance (get-balance-internal account))
    (balance-check (asserts! (>= balance amount) ERR-INSUFFICIENT-BALANCE))
    (available-assets (get-available-assets))
    (inkind (convert-to-assets-preview amount)))

  (asserts! (>= current-assets inkind) ERR-INSUFFICIENT-ASSETS)
  (asserts! (not (get redeem states)) ERR-PAUSED)
  (asserts! (> amount u0) ERR-AMOUNT-ZERO)
  (asserts! (> inkind u0) ERR-OUTPUT-ZERO)
  (asserts! (>= inkind min-out) ERR-SLIPPAGE)
  (asserts! (>= available-assets inkind) ERR-INSUFFICIENT-LIQUIDITY)

  (try! (ft-burn? zft amount account))
  (try! (send-underlying inkind recipient))
  (var-set assets (- current-assets inkind))
```

**File:** mainnet/contracts/vault/v0-vault-usdc.clar (L863-898)
```text
(define-public (system-borrow (amount uint) (receiver principal))
  (let (
      (states (var-get pause-states))
      (u (try! (accrue)))
      (CAP-DEBT (var-get cap-debt))
      (available-assets (get-available-assets))
      (scaled-principal (var-get principal-scaled))
      (idx (var-get index))
      (debt (total-debt))
      (scaled-amount (mul-div-up amount INDEX-PRECISION idx))
      (updated-scaled-principal (+ scaled-principal scaled-amount)))

    (try! (check-caller-auth))
    (asserts! (not (get borrow states)) ERR-PAUSED)
    (asserts! (> amount u0) ERR-AMOUNT-ZERO)
    (asserts! (<= amount available-assets) ERR-INSUFFICIENT-VAULT-LIQUIDITY)
    (asserts! (<= (+ debt amount) CAP-DEBT) ERR-DEBT-CAP-EXCEEDED)

    (var-set principal-scaled updated-scaled-principal)
    (var-set total-borrowed (+ (var-get total-borrowed) amount))
    (try! (send-underlying amount receiver))

    (print {
      action: "system-borrow",
      caller: contract-caller,
      data: {
        receiver: receiver,
        amount: amount,
        scaled-amount: scaled-amount,
        principal-scaled: updated-scaled-principal,
        total-borrowed: (var-get total-borrowed),
        index: idx
      }
    })

    (ok true)))
```
