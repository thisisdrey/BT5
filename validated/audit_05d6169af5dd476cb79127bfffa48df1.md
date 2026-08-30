### Title
Vault `initialize()` lacks DAO access control and can be front-run before the deployment proposal executes it - ([File: mainnet/contracts/vault/v0-vault-sbtc.clar])

### Summary
Every vault contract's `initialize` function bootstraps the vault by minting `MINIMUM-LIQUIDITY` shares to `NULL-ADDRESS`, gated only by an "already initialized" flag, with no DAO/owner access control — unlike every other administrative function in the same contract (`set-authorized-contract`, `set-flashloan-permissions`, `set-cap-supply`, etc.), which all call `check-dao-auth`. This mirrors the Velodrome `Bribe.setGauge` pattern: a function meant to be run exactly once as part of an atomic deployment/init sequence, but left callable by anyone.

### Finding Description
In `initialize`, the only guard is the one-time flag: [1](#0-0) 

```
(define-public (initialize)
  (begin
    (asserts! (not (var-get initialized)) ERR-ALREADY-INITIALIZED)
    (var-set initialized true)
    (try! (deposit MINIMUM-LIQUIDITY u0 NULL-ADDRESS))
    ...
    (ok true)))
```

This is identical across all vaults (`v0-vault-sbtc.clar`, `v0-vault-stx.clar`, `v0-vault-usdc.clar`, `v0-vault-usdh.clar`, `v0-vault-ststx.clar`, `v0-vault-ststxbtc.clar`), each showing the same unguarded pattern. [2](#0-1) 

By contrast, every other state-changing configuration entry point in the same file requires DAO authorization, e.g.: [3](#0-2) 

```
(define-public (set-authorized-contract (contract principal) (authorized bool))
  (begin
    (try! (check-dao-auth))
    ...
```

The intended flow is that the DAO's deployment proposal calls `initialize` on each vault as one step in a larger atomic sequence (asset registration, `market-vault set-impl`, cap configuration, market authorization, egroup creation), analogous to the Velodrome `Gauge` constructor calling `Bribe.setGauge`. Because `initialize` itself has no ownership/DAO check, if the vault contract is deployed and reachable on-chain before the DAO's initialization proposal transaction executes, any unprivileged principal can call `initialize` first with only `MINIMUM-LIQUIDITY` (a tiny, fixed amount) of the underlying token.

### Impact Explanation
If an attacker front-runs `initialize`, the flag becomes permanently `true`. When the DAO's init proposal later executes and reaches its own `(contract-call? .v0-vault-sbtc initialize)` step (as part of the atomic multi-step deployment sequence), that call reverts with `ERR-ALREADY-INITIALIZED` via `try!`, which aborts the *entire* proposal transaction — including asset enablement, cap configuration, `market-vault` implementation wiring, vault authorization, and egroup creation for every asset bundled in that single proposal. Since DAO proposal scripts are deployed as immutable contracts, this forces the DAO to author, deploy, and re-vote/re-execute a new proposal that accounts for the already-set `initialized` flag — a protocol-wide launch delay. This is a temporary freezing of protocol funds/functionality (users cannot supply, borrow, or have their collateral/egroups configured until the corrected proposal is redeployed and executed), matching the in-scope "temporary freezing of funds" impact class, consistent with the referenced report's Medium severity for the analogous Velodrome bug.

### Likelihood Explanation
Likelihood is moderate: it requires only that the vault contract be deployed and publicly callable before the DAO's atomic initialization proposal transaction is broadcast/confirmed — a realistic window on a public chain (deployment and proposal execution are typically separate transactions, giving an attacker a mempool/block window to submit `initialize` first for a negligible cost, `MINIMUM-LIQUIDITY`).

### Recommendation
Add DAO/owner-gated access control to `initialize` (call `check-dao-auth`, mirroring every other configuration function in the vault), or restrict it to be callable only by a designated deployer/factory address, so it cannot be triggered by an arbitrary principal ahead of the intended atomic deployment sequence.

### Proof of Concept
1. Vault contract (e.g., `v0-vault-sbtc.clar`) is deployed on-chain but the DAO's init proposal transaction (which will call `initialize` as one of its steps) has not yet been confirmed.
2. Attacker (unprivileged principal) directly calls `(contract-call? .v0-vault-sbtc initialize)`, supplying `MINIMUM-LIQUIDITY` of the underlying token from their own balance; `initialized` is set to `true`.
3. When the DAO's proposal transaction is later mined and reaches `(try! (contract-call? .v0-vault-sbtc initialize))` inside its atomic sequence, it now returns `ERR-ALREADY-INITIALIZED`, and the whole proposal transaction reverts, blocking asset enablement, cap configuration, `market-vault` wiring, and egroup creation bundled in that same proposal.
4. The DAO must draft, deploy, and pass a new proposal that skips the already-completed `initialize` call, delaying protocol launch/configuration.

### Citations

**File:** mainnet/contracts/vault/v0-vault-sbtc.clar (L494-511)
```text
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

;; -- Auth management --------------------------------------------------------

(define-public (set-authorized-contract (contract principal) (authorized bool))
```

**File:** mainnet/contracts/vault/v0-vault-sbtc.clar (L513-529)
```text
    (try! (check-dao-auth))
    
    (print {
      action: "vault-set-authorized-contract",
      caller: tx-sender,
      data: {
        vault: UNDERLYING,
        contract: contract,
        authorized: authorized
      }
    })
    
    (ok (map-set authorized-contracts contract authorized))))

;; -- Flashloan permissions --------------------------------------------------

(define-public (set-flashloan-permissions 
```

**File:** mainnet/contracts/vault/v0-vault-stx.clar (L492-509)
```text
;; -- Initialization ---------------------------------------------------------

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
