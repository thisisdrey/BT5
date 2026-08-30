### Title
Deposits into an insolvent vault (total-assets == 0 via `socialize-debt`) mint zero shares while the depositor's funds are absorbed by remaining shareholders — (`File: mainnet/contracts/vault/v0-vault-stx.clar` and sibling vault contracts)

### Summary
The vault contracts' `convert-to-shares-preview` returns `u0` shares whenever `total-assets-preview` is zero, instead of reverting. Because `socialize-debt` can legitimately drive the vault's `assets` variable (and hence `total-assets`) to zero while `total-supply` of the vToken (`zft`) remains non-zero, a subsequent `deposit` can silently mint zero shares to the depositor while still crediting the deposited amount to the vault's `assets`, transferring value to the remaining shareholders at the depositor's expense.

### Finding Description
`convert-to-shares-preview` computes shares for a deposit as: [1](#0-0) 

```clarity
(define-private (convert-to-shares-preview (amount uint))
  (let ((ta (total-assets-preview))
        (ts (total-supply-preview)))
    (if (is-eq ts u0)
        amount
        (if (is-eq ta u0)
            u0
            (mul-div-down amount ts ta)))))
```

When `ts > 0` (i.e. existing vToken holders) but `ta` (total assets) is `0`, the function returns `u0` instead of reverting or handling the insolvent state specially.

`total-assets` can reach `0` through the normal, permissioned `socialize-debt` flow, which is intended to write off bad debt after a shortfall: [2](#0-1) 

```clarity
(define-public (socialize-debt (scaled-amount uint))
  (let ((scaled-principal (var-get principal-scaled))
        ...
        (old-total-assets (total-assets))
        ...)
    (try! (check-caller-auth))
    (asserts! (> scaled-amount u0) ERR-AMOUNT-ZERO)
    (var-set lindex new-lindex)
    (var-set principal-scaled (if (> scaled-principal scaled-amount) (- scaled-principal scaled-amount) u0))
    (var-set total-borrowed (if (> borrowed principal-reduction) (- borrowed principal-reduction) u0))
    (var-set assets (if (> current-assets principal-reduction) (- current-assets principal-reduction) u0))
    ...))
```

If `socialize-debt` writes off the full outstanding scaled principal, `principal-scaled`, `total-borrowed` and `assets` are all zeroed. `total-debt`/`total-assets` are then derived purely from these state variables: [3](#0-2) 

```clarity
(define-private (total-assets)
  (let ((current-assets (var-get assets))
        (debt (total-debt))
        (borrowed (var-get total-borrowed))
        (interest (if (> debt borrowed) (- debt borrowed) u0)))
    (+ current-assets interest)))
```

With `assets = 0`, `principal-scaled = 0`, `total-borrowed = 0`, `total-assets` (and its preview) becomes `0`, while the vToken `total-supply` is untouched by `socialize-debt` — existing depositors still hold their (now worthless) vTokens.

At this point, any user calling `deposit` will have `convert-to-shares-preview` return `0`: [4](#0-3) 

```clarity
(define-public (deposit (amount uint) (min-out uint) (recipient principal))
    (let (
      ...
      (inkind (convert-to-shares-preview amount)))
    ...
    (asserts! (>= inkind min-out) ERR-SLIPPAGE)
    ...
    (try! (receive-underlying amount account))
    (try! (ft-mint? zft inkind recipient))
    (var-set assets (+ current-assets amount))
    ...
    (ok inkind)))
```

If the caller passes `min-out u0` (a very plausible default for a first deposit, for scripted/integrator callers, or for anyone unaware the vault has become insolvent), the deposit succeeds with `inkind = u0`: the depositor's real underlying funds are pulled in via `receive-underlying` and immediately added to `assets`, but `ft-mint?` mints zero shares to them. The deposited value is thus absorbed entirely into the shared asset pool, instantly and proportionally benefiting every existing (impaired) vToken holder — a direct transfer of the new depositor's funds to other users with no compensation.

This is the structural analog of the referenced report: a share/asset accounting formula degenerates to `0` under a specific, reachable state (there: `oldShares == totalSupply`; here: `totalAssets == 0` while `totalSupply > 0`), silently producing an incorrect (zero) share allocation instead of reflecting/rejecting the true, non-trivial value transferred.

This bug pattern is identical across all vault instances (`v0-vault-stx.clar`, `v0-vault-sbtc.clar`, `v0-vault-usdc.clar`, `v0-vault-usdh.clar`, `v0-vault-ststx.clar`, `v0-vault-ststxbtc.clar`), since they all share the same `convert-to-shares-preview` / `socialize-debt` logic.

### Impact Explanation
This is a direct theft of user funds at rest/in motion: a depositor's transferred underlying asset is absorbed by the vault while the depositor receives no corresponding share of ownership (zero vTokens). Their funds effectively transfer to the other, pre-existing (impaired) vToken holders. This maps to the Critical impact class — direct theft of user funds.

### Likelihood Explanation
`socialize-debt` fully zeroing `assets`/`total-borrowed`/`principal-scaled` is the expected end-state of the protocol's designed bad-debt write-off mechanism after a severe under-collateralized liquidation shortfall — not a contrived or purely theoretical state. Once the vault is in this state, any subsequent depositor who does not set a protective `min-out` (a common integration/UX default) is silently robbed on their very next `deposit` call, an unprivileged/ordinary user action requiring no special permissions.

### Recommendation
`convert-to-shares-preview` should not silently return `u0` when `ta == 0` and `ts > 0`. Either:
- Revert the deposit entirely when the vault is in this insolvent state (e.g. via a dedicated error such as `ERR-VAULT-INSOLVENT`), or
- Explicitly handle write-down/re-basing of `total-supply` in tandem with `socialize-debt` so that the vault's share price cannot degenerate to a state where deposits mint zero shares for a nonzero contribution.

At minimum, `deposit` should treat `inkind == u0` as always invalid (i.e., assert `inkind > u0` regardless of the caller-supplied `min-out`), matching the analogous safety check already present on the `redeem` path (`(asserts! (> inkind u0) ERR-OUTPUT-ZERO)`), which `deposit` currently lacks.

### Proof of Concept
1. Vault (e.g. `v0-vault-stx.clar`) is operating normally with several depositors holding vTokens (`total-supply > 0`), and the vault has an active borrow position (`principal-scaled > 0`, `total-borrowed > 0`).
2. A borrower default/shortfall occurs; the authorized caller (per `check-caller-auth`) invokes `socialize-debt` with `scaled-amount` equal to the full outstanding `principal-scaled`. This sets `principal-scaled = 0`, `total-borrowed = 0`, and `assets = 0` (assuming `principal-reduction >= current-assets`), per [5](#0-4) .
3. `total-assets`/`total-assets-preview` now evaluate to `0`, while `total-supply` (zft) remains unchanged and non-zero.
4. A new user calls `deposit(amount, min-out=u0, recipient=self)`. `convert-to-shares-preview(amount)` returns `u0` because `ta == 0` and `ts > 0`, per [6](#0-5) .
5. The `(asserts! (>= inkind min-out) ERR-SLIPPAGE)` check passes trivially since `min-out = 0`. `receive-underlying` pulls the user's `amount` of underlying tokens into the vault; `ft-mint?` mints `0` vTokens to the user; `assets` is increased by `amount`.
6. The depositor now holds `0` vTokens (total loss of their deposited principal), while every pre-existing vToken holder's redeemable value increases proportionally because `total-assets` grew by `amount` with no corresponding increase in `total-supply`.

### Citations

**File:** mainnet/contracts/vault/v0-vault-stx.clar (L308-315)
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

**File:** mainnet/contracts/vault/v0-vault-stx.clar (L332-339)
```text
  (calc-cumulative-debt (var-get principal-scaled) (next-index)))

(define-private (total-assets)
  (let ((current-assets (var-get assets))
        (debt (total-debt))
        (borrowed (var-get total-borrowed))
        (interest (if (> debt borrowed) (- debt borrowed) u0)))
    (+ current-assets interest)))
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

**File:** mainnet/contracts/vault/v0-vault-stx.clar (L944-967)
```text
(define-public (socialize-debt (scaled-amount uint))
  (let ((scaled-principal (var-get principal-scaled))
        (borrowed (var-get total-borrowed))
        (idx (var-get index))
        (current-assets (var-get assets))
        (current-lindex (var-get lindex))
        (old-total-assets (total-assets))
        (debt-reduction (mul-div-down scaled-amount idx INDEX-PRECISION))
        (principal-reduction (if (> scaled-principal u0)
                                (mul-div-down scaled-amount borrowed scaled-principal)
                                u0))
        ;; Write down lindex proportionally to loss in total-assets
        (new-lindex (if (and (> old-total-assets u0) (> old-total-assets debt-reduction))
                       (mul-div-down current-lindex (- old-total-assets debt-reduction) old-total-assets)
                       u0)))

    (try! (check-caller-auth))
    (asserts! (> scaled-amount u0) ERR-AMOUNT-ZERO)

    (var-set lindex new-lindex)
    (var-set principal-scaled (if (> scaled-principal scaled-amount) (- scaled-principal scaled-amount) u0))
    (var-set total-borrowed (if (> borrowed principal-reduction) (- borrowed principal-reduction) u0))
    (var-set assets (if (> current-assets principal-reduction) (- current-assets principal-reduction) u0))

```
