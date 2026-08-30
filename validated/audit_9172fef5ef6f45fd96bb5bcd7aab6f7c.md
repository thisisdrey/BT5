### Title
Unauthenticated `initialize()` on vaults can be front-run to permanently block the DAO's protocol-initialization proposal - ([File: mainnet/contracts/vault/v0-vault-sbtc.clar])

### Summary
The vault contracts (`v0-vault-sbtc.clar`, `v0-vault-stx.clar`, `v0-vault-usdc.clar`, `v0-vault-usdh.clar`, `v0-vault-ststx.clar`, `v0-vault-ststxbtc.clar`) expose a `public` `initialize` function that is guarded only by a one-time `initialized` boolean, with **no DAO/authorized-caller check** at all. Any unprivileged account holding a small amount of the underlying token can call it before the DAO's own `v0-init` proposal does, permanently setting `initialized` to `true` and causing the DAO's genesis-initialization proposal (which itself calls `initialize`) to revert. This mirrors the referenced Morpho report's root cause: an entry point that creates/sets up a piece of protocol state has no check for "has this already been done" that is race-safe against an unprivileged front-runner, so the legitimate, privileged initialization transaction can be griefed.

### Finding Description
`initialize` is defined without any `check-dao-auth` (or equivalent) call: [1](#0-0) 

```
(define-public (initialize)
  (begin
    (asserts! (not (var-get initialized)) ERR-ALREADY-INITIALIZED)
    (var-set initialized true)
    (try! (deposit MINIMUM-LIQUIDITY u0 NULL-ADDRESS))
    ...
    (ok true)))
``` [2](#0-1) 

The only guard is the `initialized` data-var check; there is no `is-eq tx-sender .dao-executor` assertion the way every other privileged setter in the same file has (`set-authorized-contract`, `set-flashloan-permissions` both call `check-dao-auth`). The intended flow (seen in the DAO proposal script) is that the DAO's `v0-init` proposal calls `initialize` on each vault as part of a single orchestrated setup transaction that also wires up `market-vault` implementation, sets caps, and authorizes the market contract: [3](#0-2) 

Because `initialize` is public and unauthenticated, any account can watch the mempool/anticipate the DAO proposal and call `initialize` first (they only need `MINIMUM-LIQUIDITY` (1000 base units) of the underlying token, which they can acquire independently of the DAO). Once `initialized` flips to `true`, the DAO's subsequent call to the same function inside its all-or-nothing proposal reverts with `ERR-ALREADY-INITIALIZED`, causing the entire proposal transaction (asset registration, cap configuration, market authorization, egroup creation, etc., all chained with `try!`) to abort atomically. This is the same class of bug as the Morpho `createMarket` issue: an entry point meant to be executed once by a privileged/expected party can be raced and blocked by an unprivileged actor because the "already done" check is the only gate and is reachable by anyone.

### Impact Explanation
This is a Medium-severity DoS on protocol deployment/initialization: it does not let the attacker steal funds or gain vault shares (the minimum-liquidity shares are minted to `NULL-ADDRESS`, not the caller), but it forces the DAO to re-submit and re-sequence its initialization proposal, delaying protocol launch and wasting DAO signer coordination/gas. Under the strict impact taxonomy given (Critical: theft/insolvency/permanent freeze; High: theft or freezing of unclaimed yield/royalties, or temporary freezing of funds), this bug does not by itself freeze any *existing* user funds — it blocks a not-yet-active protocol from ever coming online, or delays such an event without freezing funds that were already deposited. Because no user funds are at risk (the protocol isn't live for depositors before initialization completes), this finding does not clearly land in the required Critical/High impact classes for this scan; it is closer to a deployment/DoS annoyance than fund-freezing.

### Likelihood Explanation
Likelihood is low-to-medium: the attacker needs to know the address of an about-to-be-deployed vault (predictable since Clarity contract names/addresses in these deployments are deterministic and typically public before the DAO proposal executes) and needs a small amount of the underlying token. Because the vaults are deployed but not yet initialized, and the exact underlying token address is a public constant in the contract, this is technically executable by any observer of the deployment plan.

### Recommendation
Add the same `check-dao-auth` (or an equivalent explicit allow-list check) to `initialize` that is already used for `set-authorized-contract` and `set-flashloan-permissions` in the same contracts, so that only the DAO executor can trigger the one-time initialization, matching the Morpho recommendation of not permitting arbitrary front-running of a not-yet-created/initialized resource by an unprivileged principal.

### Proof of Concept
1. Vault contract `v0-vault-sbtc` is deployed but the DAO's `v0-init` proposal has not yet been executed.
2. Attacker acquires `MINIMUM-LIQUIDITY` (1000 base units) of sBTC and calls `(contract-call? .v0-vault-sbtc initialize)` directly, which succeeds because there is no auth check — only `(asserts! (not (var-get initialized)) ERR-ALREADY-INITIALIZED)`.
3. `initialized` is now `true`.
4. The DAO executes its `v0-init` proposal, which (among other steps) calls `(try! (contract-call? .v0-vault-sbtc initialize))` as one line in a long chain of `try!` calls. This call now returns `ERR-ALREADY-INITIALIZED`, and because of `try!`, the entire proposal transaction reverts.
5. The DAO must redeploy/re-author a new initialization proposal that skips the already-initialized vault, delaying protocol launch. [1](#0-0) [4](#0-3)

### Citations

**File:** mainnet/contracts/vault/v0-vault-sbtc.clar (L44-46)
```text
(define-constant ERR-AUTH (err u801001))
(define-constant ERR-INIT (err u801002))
(define-constant ERR-ALREADY-INITIALIZED (err u801003))
```

**File:** mainnet/contracts/vault/v0-vault-sbtc.clar (L492-507)
```text
(define-public (initialize)
  (begin
    (asserts! (not (var-get initialized)) ERR-ALREADY-INITIALIZED)
    (var-set initialized true)
    (try! (deposit MINIMUM-LIQUIDITY u0 NULL-ADDRESS))
    
    (print {
      action: "vault-initialize",
      caller: contract-caller,
      data: {
        vault: UNDERLYING,
        minimum-liquidity: MINIMUM-LIQUIDITY
      }
    })
    
    (ok true)))
```

**File:** mainnet/contracts/proposals/mainnet/v0-init.clar (L157-184)
```text
    ;; STEP 2: INITIALIZE MARKET-VAULT
    (try! (contract-call? .v0-market-vault set-impl .v0-1-market))
    
    ;; STEP 3: CONFIGURE VAULTS
    
    ;; Set vault caps
    (try! (contract-call? .v0-vault-stx set-cap-supply CAP-STX-SUPPLY))
    (try! (contract-call? .v0-vault-stx set-cap-debt CAP-STX-DEBT))
    (try! (contract-call? .v0-vault-sbtc set-cap-supply CAP-SBTC-SUPPLY))
    (try! (contract-call? .v0-vault-sbtc set-cap-debt CAP-SBTC-DEBT))
    (try! (contract-call? .v0-vault-ststx set-cap-supply CAP-STSTX-SUPPLY))
    (try! (contract-call? .v0-vault-ststx set-cap-debt CAP-STSTX-DEBT))
    (try! (contract-call? .v0-vault-usdc set-cap-supply CAP-USDC-SUPPLY))
    (try! (contract-call? .v0-vault-usdc set-cap-debt CAP-USDC-DEBT))
    (try! (contract-call? .v0-vault-usdh set-cap-supply CAP-USDH-SUPPLY))
    (try! (contract-call? .v0-vault-usdh set-cap-debt CAP-USDH-DEBT))
    (try! (contract-call? .v0-vault-ststxbtc set-cap-supply CAP-STSTXBTC-SUPPLY))
    (try! (contract-call? .v0-vault-ststxbtc set-cap-debt CAP-STSTXBTC-DEBT))
    
    ;; Authorize market contract in all vaults
    (try! (contract-call? .v0-vault-stx set-authorized-contract .v0-1-market true))
    (try! (contract-call? .v0-vault-sbtc set-authorized-contract .v0-1-market true))
    (try! (contract-call? .v0-vault-ststx set-authorized-contract .v0-1-market true))
    (try! (contract-call? .v0-vault-usdc set-authorized-contract .v0-1-market true))
    (try! (contract-call? .v0-vault-usdh set-authorized-contract .v0-1-market true))
    (try! (contract-call? .v0-vault-ststxbtc set-authorized-contract .v0-1-market true))
    
    ;; STEP 4: CREATE EGROUPS
```
