### Title
Reentrancy via attacker-controlled `<ft-trait>` in `collateral-add` allows phantom-collateral borrow before token transfer settles — ([File: mainnet/contracts/market/v0-market-vault.clar])

### Summary
This is a Clarity analog of the ERC-777 reentrancy bug: `collateral-add` in `v0-market-vault.clar` mutates the user's collateral ledger *before* it validates preconditions and *before* it calls out to the attacker-supplied SIP-010-style `<ft-trait>` contract to actually pull in the tokens. Because the trait parameter is caller-controlled, an attacker can deploy a malicious "token" contract that reenters `market.clar`'s `borrow()` from within its own `transfer` hook and drain real underlying assets against collateral that was never actually deposited.

### Finding Description
`collateral-add` computes and *commits* the new collateral balance as part of its `let` bindings, which in Clarity are evaluated eagerly and sequentially, before any `asserts!`/`try!` checks in the function body run: [1](#0-0) 

The state mutation itself (`map-set collateral ...`) happens inside `add-user-collateral`: [2](#0-1) 

Only *after* this write does the function check `check-impl-auth`, the pause flag, `amount > 0`, and finally invoke the external transfer via `receive-tokens`, which calls `transfer` on the caller-supplied `<ft-trait>` contract: [3](#0-2) 

Because `ft` is a generic trait reference (any contract implementing `ft-trait` can be passed — see `market-trait.clar`'s `collateral-add (<ft-trait> uint ...)`), an attacker can supply their own malicious contract as `ft`. That contract's `transfer` function is invoked mid-transaction, at a point where the collateral map already reflects the *new, inflated* balance — even though no real tokens have moved yet. From inside that hook the attacker's contract can re-enter `market.clar`'s public `borrow()` (using itself as `contract-caller`/`account`, since it initiated the whole call chain), which reads the position via `get-position`/`market-vault` storage (now showing the phantom collateral), passes the health check, and calls `vault-system-borrow` → `send-underlying`, which transfers **real** underlying assets out of the vault to the attacker. The reentrant `transfer` hook can then simply return `(ok true)` without moving any real tokens, and the outer `collateral-add` call completes successfully.

This mirrors the reported bug class precisely: an externally-invoked hook (ERC-777 `tokensToSend`/`operatorSend` there, SIP-010 `<ft-trait> transfer` here) is reachable mid-function, and a state variable that determines pricing/collateralization (`FAIR.totalSupply` there, the `collateral` map here) is mutated on the "wrong side" of that external call, letting the attacker act on stale/premature state before the real value transfer is settled.

### Impact Explanation
This lands squarely in **Critical**: an attacker can borrow real underlying assets (STX/sBTC/USDC/USDH/etc.) against collateral that was never actually transferred into the protocol, directly stealing user/protocol funds and leaving the vault undercollateralized — i.e., protocol insolvency, matching the “direct theft of user funds… or protocol insolvency” impact bucket.

### Likelihood Explanation
The entry point (`collateral-add`) is callable by any ordinary, unprivileged principal (no DAO/privileged role required), and the `<ft-trait>` parameter is fully attacker-controlled — the attacker only needs to deploy one small contract implementing the trait. No oracle manipulation, flashloan, or governance action is needed; the only prerequisite is that the attacker's account can pass the (minimal) new-user egroup validation in `market.clar`'s `collateral-add`, which is trivially satisfied for a fresh account with no existing debt.

### Recommendation
- Move all state-mutating map/data-var writes (e.g., `add-user-collateral`) to occur strictly *after* all `asserts!`/`try!` preconditions and *after* the external `receive-tokens` transfer call has succeeded (checks-effects-interactions ordering), not inside eagerly-evaluated `let` bindings that run ahead of validation.
- Add an explicit reentrancy guard (a `bool` data-var checked/set similarly to the existing `in-flashloan` guard used elsewhere in the vault contracts) around `collateral-add`/`collateral-remove` and any other function that performs a `contract-call?` to a caller-supplied `<ft-trait>` before finalizing ledger state.
- Ensure `receive-tokens`/token pull-in always happens before any position/collateral accounting is updated, consistent with the vault contracts' `deposit` pattern (`receive-underlying` then `ft-mint?` then `var-set assets`), which should be applied consistently to `market-vault.clar`.

### Proof of Concept
1. Attacker deploys `EvilToken`, a contract implementing `ft-trait`, whose `transfer` function ignores the actual token movement and instead calls `(contract-call? .market borrow .vault-usdc BORROW_AMOUNT none none)`.
2. Attacker (as `EvilToken` or via a wrapper that makes `EvilToken` the `contract-caller`) calls `market.clar collateral-add(EvilToken, COLLATERAL_AMOUNT, none)`.
3. `market.clar collateral-add` passes its lightweight new-user checks and calls `.market-vault collateral-add(account, COLLATERAL_AMOUNT, EvilToken, asset-id)`.
4. Inside `market-vault.clar collateral-add`, `add-user-collateral` writes `collateral[account] += COLLATERAL_AMOUNT` as part of the `let` bindings — before `check-impl-auth`, pause check, and before any token is received.
5. `receive-tokens` is called, invoking `EvilToken.transfer`, which reenters `market.clar borrow`. The health check reads the now-inflated (but not actually funded) collateral and succeeds; `vault-system-borrow` sends real underlying tokens to the attacker.
6. `EvilToken.transfer` returns `(ok true)` without ever moving real tokens; `collateral-add` completes normally.
7. Net effect: attacker walks away with real borrowed assets backed by collateral that was never deposited.

### Citations

**File:** mainnet/contracts/market/v0-market-vault.clar (L198-203)
```text
(define-private (add-user-collateral (user-id uint) (asset-id uint) (amount uint))
  (let ((key { id: user-id, asset: asset-id })
        (collateral-amount (default-to u0 (map-get? collateral key))) ;; graceful default
        (updated-collateral-amount (+ collateral-amount amount)))
      (map-set collateral key updated-collateral-amount)
      updated-collateral-amount))
```

**File:** mainnet/contracts/market/v0-market-vault.clar (L256-257)
```text
(define-private (receive-tokens (asset <ft-trait>) (amount uint) (account principal))
  (contract-call? asset transfer amount account current-contract none))
```

**File:** mainnet/contracts/market/v0-market-vault.clar (L374-404)
```text
(define-public (collateral-add (account principal) (amount uint) (ft <ft-trait>) (asset-id uint))
  (let ((states (var-get pause-states))
        (entry (resolve-or-create account))
        (user-id (get id entry))
        (mask (get mask entry))
        (updated-mask (mask-update mask asset-id true true)) ;; collateral, insert
        (updated-entry (merge entry (refresh updated-mask)))
        (result (add-user-collateral user-id asset-id amount)))

    (try! (check-impl-auth))
    (asserts! (not (get collateral-add states)) ERR-PAUSED)
    (asserts! (> amount u0) ERR-AMOUNT-ZERO)

    (try! (receive-tokens ft amount account))
    
    (insert updated-entry)

    (print {
      action: "collateral-add",
      caller: contract-caller,
      data: {
        account: account,
        asset-id: asset-id,
        amount: amount,
        updated-collateral-amount: result,
        mask-before: mask,
        mask-after: updated-mask
      }
    })
      
    (ok result)))
```
