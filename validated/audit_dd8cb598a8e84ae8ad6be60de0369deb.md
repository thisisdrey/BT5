### Title
Vault `deposit` allows minting zero shares for a rounded-down amount, permanently absorbing the depositor's underlying tokens - ([File: mainnet/contracts/vault/v0-vault-stx.clar])

### Summary
The external report describes tokens being bridged in amounts smaller than the destination's minimum denomination, causing `scaleTokens` to round to zero minted tokens while the source chain still locks the full amount, permanently losing the user's funds. The same root-cause pattern — an unprotected division/scaling step that can round a nonzero input down to a zero output while the input value is still consumed — exists in the Zest vault share-math conversion used by `deposit`.

### Finding Description
`convert-to-shares-preview` computes shares as `mul-div-down amount ts ta` whenever `ts > 0` and `ta > 0`: [1](#0-0) 

Once a vault has accrued interest (share price `ta/ta` > 1), a deposit `amount` small enough that `amount * ts < ta` rounds down to `0` shares. The `deposit` function only guards `amount > 0` and checks `inkind >= min-out` for slippage, but never asserts `inkind > 0`: [2](#0-1) 

If the caller (or a naive integrating contract) supplies `min-out = 0` — the natural default when a caller isn't computing an exact expected-shares figure — the slippage check `(>= inkind min-out)` trivially passes even when `inkind` is `0`. The function then proceeds to pull the full `amount` of underlying from the depositor via `receive-underlying`, mints `0` shares via `ft-mint? zft inkind recipient` (a no-op), and increases `assets` by the full deposited `amount`. The depositor's tokens are absorbed into the vault's `assets` balance with zero shares issued in return — an unrecoverable loss for that depositor, and the vault's other shareholders benefit passively from the incremental assets that back their existing shares. This is functionally identical to the Teleporter bug class: a conversion function can silently truncate a nonzero input to a zero output, while the "locking" side effect (the underlying transfer) is unconditionally executed regardless of the result. Notably, `redeem` in the same contract does guard against this (`asserts! (> inkind u0) ERR-OUTPUT-ZERO)`), confirming the pattern is a known necessary check in this codebase that was simply omitted from `deposit`: [3](#0-2) 

The same `deposit` structure (missing `inkind > 0` check) is duplicated across all mainnet vaults: `v0-vault-stx.clar`, `v0-vault-sbtc.clar`, `v0-vault-ststx.clar`, `v0-vault-usdc.clar`, `v0-vault-usdh.clar`, `v0-vault-ststxbtc.clar`. [4](#0-3) [5](#0-4) 

### Impact Explanation
The depositor's underlying tokens are transferred into the vault while the depositor receives no zTokens/shares in exchange, and there is no recovery mechanism for the zero-share deposit — matching "Critical: direct theft/permanent freezing of funds at rest." The funds are not destroyed but are irrecoverably and permanently transferred out of the depositor's control into the collective vault balance with no compensating claim, which functions as a permanent loss of principal for that user each time it occurs.

### Likelihood Explanation
This requires (a) the vault to have already accrued interest so `ta/ts > 1` (a normal steady-state condition for active vaults), and (b) a deposit call whose `amount` is small enough, combined with `min-out = 0` (or a min-out that still permits `inkind = 0`, i.e., `min-out = 0`). This is easily triggered by any unprivileged caller or their own deployed contract sending a dust-sized deposit, or by third-party integrations/wrappers that default `min-out` to `0` for convenience. It requires no privileged action and no oracle manipulation, matching an ordinary-principal-reachable path through `deposit`.

### Recommendation
Add an explicit `(asserts! (> inkind u0) ERR-OUTPUT-ZERO)` check to `deposit` in every vault contract, mirroring the existing check already present in `redeem`, so that deposits which would round to zero shares are rejected before the underlying transfer occurs.

### Proof of Concept
1. Vault accrues interest over time so that `total-assets-preview() / total-supply-preview() > 1` (e.g., ratio 2:1).
2. Attacker/ordinary user calls `deposit(amount, min-out=0, recipient)` with a small `amount` such that `mul-div-down(amount, ts, ta) == 0` (e.g., `amount * ts < ta`).
3. `deposit` passes all assertions (`amount > 0` true; `inkind (0) >= min-out (0)` true; supply cap not exceeded).
4. `receive-underlying` pulls the full `amount` of underlying tokens from the caller.
5. `ft-mint? zft 0 recipient` mints zero shares.
6. `assets` is incremented by `amount`, permanently absorbing the depositor's funds with no shares issued and no way for the depositor to reclaim them.

### Citations

**File:** local-testing/contracts/vault/vault-stx.clar (L308-315)
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

**File:** mainnet/contracts/vault/v0-vault-stx.clar (L763-795)
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
```

**File:** mainnet/contracts/vault/v0-vault-stx.clar (L797-811)
```text
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

  (asserts! (>= current-assets inkind) ERR-INSUFFICIENT-ASSETS)
  (asserts! (not (get redeem states)) ERR-PAUSED)
  (asserts! (> amount u0) ERR-AMOUNT-ZERO)
  (asserts! (> inkind u0) ERR-OUTPUT-ZERO)
```

**File:** mainnet/contracts/vault/v0-vault-sbtc.clar (L761-793)
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
```

**File:** mainnet/contracts/vault/v0-vault-usdc.clar (L765-793)
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
      data: {
        depositor: account,
        recipient: recipient,
        amount: amount,
        shares-minted: inkind,
        assets: (+ current-assets amount)
      }
    })

    (ok inkind)))
```
