### Title
Front-runnable `initialize()` in vault contracts allows DOS of DAO protocol-launch/upgrade proposals - (File: `mainnet/contracts/vault/v0-vault-stx.clar` and sibling vault contracts)

### Summary
Every vault contract (`v0-vault-stx.clar`, `v0-vault-sbtc.clar`, `v0-vault-ststx.clar`, `v0-vault-usdc.clar`, `v0-vault-usdh.clar`, `v0-vault-ststxbtc.clar`, and their `local-testing` counterparts) exposes a public `initialize` function that is guarded only by an "already initialized" check, not by DAO authorization. Any unprivileged principal can call it before the DAO's governance proposal does, causing the DAO's atomic multi-step proposal to revert entirely — the same DOS pattern as the referenced RocketJoe `LaunchEvent`/`JoePair` finding, where a permissionless action that "already exists"-guards a critical piece of protocol state can be front-run to block the intended privileged initialization.

### Finding Description
The `initialize` function in the vault contracts is: [1](#0-0) 

```clarity
(define-public (initialize)
  (begin
    (asserts! (not (var-get initialized)) ERR-ALREADY-INITIALIZED)
    (var-set initialized true)
    (try! (deposit MINIMUM-LIQUIDITY u0 NULL-ADDRESS))
    ...
    (ok true)))
```

Unlike every other privileged setter in the same contract — `set-cap-debt`, `set-cap-supply`, `set-fee-flash`, `set-default-flashloan-permissions`, etc. — which all begin with `(try! (check-dao-auth))`, `initialize` has **no** DAO-auth or caller-auth check: [2](#0-1) 

The only gate is the boolean `initialized` var, exactly mirroring the RocketJoe bug where `createPair()`'s only protection was "does a pair already exist." Any address holding a trivial amount of the underlying asset (`MINIMUM-LIQUIDITY`, e.g. `u1000` units) can call `initialize` directly on the not-yet-launched (or not-yet-upgraded) vault contract, flip `initialized` to `true`, and deposit the minimum liquidity itself.

The DAO's own governance proposals call this same function as one step inside a large atomic transaction: [3](#0-2) 

```clarity
(try! (as-contract? ((with-all-assets-unsafe))
  (try! (contract-call? .vault-sbtc initialize))
  (try! (contract-call? .vault-usdh initialize))
  (try! (contract-call? .vault-usdc initialize))
  (try! (contract-call? .vault-ststx initialize))
  (try! (contract-call? .vault-stx initialize))
))
```

and similarly inside the larger mainnet launch/upgrade proposal that also sets asset oracle registrations, vault caps, market authorization, and creates all egroups and interest-rate curves in the same atomic call: [4](#0-3) 

Because Clarity's `try!` aborts the entire transaction on the first error, if an attacker front-runs any one of the `initialize` calls, the corresponding `(try! (contract-call? .vault-X initialize))` inside the DAO proposal will hit `ERR-ALREADY-INITIALIZED` and revert the **whole** proposal — including all the unrelated steps bundled with it (asset registration, cap configuration, market authorization, egroup creation, interest-rate curve setup). This is functionally identical to the RocketJoe report: a cheap, permissionless pre-emption of a state transition that the privileged/DAO flow assumes it uniquely controls, which blocks the DAO's multi-step operation from ever succeeding as written.

### Impact Explanation
This lands in the "temporary freezing of funds" bucket: whenever the DAO bundles a new vault's `initialize()` together with configuration changes for already-live vaults (cap updates, rate-curve updates, egroup changes, market re-authorization) in a single proposal — as the `proposal-protocol-init.clar` / mainnet `v0-init.clar` pattern does — an attacker can grief that entire proposal for the cost of `MINIMUM-LIQUIDITY` of the underlying asset. Until the DAO drafts, votes on, and executes a brand-new proposal that skips the already-initialized vault, none of the bundled configuration changes take effect, temporarily freezing the market's ability to onboard the new asset or update parameters for existing vaults/users.

### Likelihood Explanation
Likelihood is high for the initial "prevent launch" scenario (trivial, cheap, requires no special access) but the severity depends on whether the DAO bundles `initialize()` calls together with unrelated live-vault changes in one atomic proposal, which the codebase's own proposal scripts show it does.

### Recommendation
Add a `check-dao-auth` (or `check-caller-auth` restricted to the DAO executor) gate to `initialize` in all vault contracts, consistent with every other administrative function in the same file, so only the DAO-controlled flow can trigger vault initialization.

### Proof of Concept
1. DAO deploys a new (or upgraded) vault contract, intending to initialize it via a bundled proposal (e.g. `proposal-init-vaults.clar` / `proposal-protocol-init.clar`).
2. Before the DAO proposal transaction is mined, an attacker (any address holding `MINIMUM-LIQUIDITY` of the vault's underlying asset) calls `(contract-call? .vault-X initialize)` directly.
3. `initialized` flips to `true` and `MINIMUM-LIQUIDITY` is deposited to `NULL-ADDRESS` from the attacker's own funds.
4. The DAO's proposal transaction executes; its `(try! (contract-call? .vault-X initialize))` step returns `ERR-ALREADY-INITIALIZED`, and the entire `begin` block — including unrelated asset registrations, cap settings, market authorizations, egroup creation, and interest-rate curve configuration bundled in the same proposal — reverts.
5. The DAO must draft and pass an entirely new proposal that omits the already-initialized vault step to complete the intended configuration changes.

### Citations

**File:** mainnet/contracts/vault/v0-vault-ststx.clar (L494-509)
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

**File:** mainnet/contracts/vault/v0-vault-stx.clar (L573-606)
```text
(define-public (set-cap-debt (val uint))
  (begin
    (try! (check-dao-auth))
    
    (print {
      action: "vault-set-cap-debt",
      caller: tx-sender,
      data: {
        vault: UNDERLYING,
        old-value: (var-get cap-debt),
        new-value: val
      }
    })
    
    (var-set cap-debt val)
    (ok true)))

(define-public (set-cap-supply (val uint))
  (begin
    (try! (check-dao-auth))
    
    (print {
      action: "vault-set-cap-supply",
      caller: tx-sender,
      data: {
        vault: UNDERLYING,
        old-value: (var-get cap-supply),
        new-value: val
      }
    })
    
    (var-set cap-supply val)
    (ok true)))

```

**File:** local-testing/contracts/proposals/proposal-init-vaults.clar (L25-33)
```text
    ;; Initialize vaults (mints minimum liquidity)
    ;; Called directly - dao-executor's with-all-assets-unsafe handles asset transfers
    (try! (as-contract? ((with-all-assets-unsafe))
      (try! (contract-call? .vault-sbtc initialize))
      (try! (contract-call? .vault-usdh initialize))
      (try! (contract-call? .vault-usdc initialize))
      (try! (contract-call? .vault-ststx initialize))
      (try! (contract-call? .vault-stx initialize))
    ))
```

**File:** local-testing/contracts/proposals/mainnet/proposal-protocol-init.clar (L159-184)
```text
    ;; STEP 2: INITIALIZE MARKET-VAULT
    (try! (contract-call? .market-vault set-impl .market))
    
    ;; STEP 3: CONFIGURE VAULTS
    
    ;; Set vault caps
    (try! (contract-call? .vault-stx set-cap-supply CAP-STX-SUPPLY))
    (try! (contract-call? .vault-stx set-cap-debt CAP-STX-DEBT))
    (try! (contract-call? .vault-sbtc set-cap-supply CAP-SBTC-SUPPLY))
    (try! (contract-call? .vault-sbtc set-cap-debt CAP-SBTC-DEBT))
    (try! (contract-call? .vault-ststx set-cap-supply CAP-STSTX-SUPPLY))
    (try! (contract-call? .vault-ststx set-cap-debt CAP-STSTX-DEBT))
    (try! (contract-call? .vault-usdc set-cap-supply CAP-USDC-SUPPLY))
    (try! (contract-call? .vault-usdc set-cap-debt CAP-USDC-DEBT))
    (try! (contract-call? .vault-usdh set-cap-supply CAP-USDH-SUPPLY))
    (try! (contract-call? .vault-usdh set-cap-debt CAP-USDH-DEBT))
    (try! (contract-call? .vault-ststxbtc set-cap-supply CAP-STSTXBTC-SUPPLY))
    (try! (contract-call? .vault-ststxbtc set-cap-debt CAP-STSTXBTC-DEBT))
    
    ;; Authorize market contract in all vaults
    (try! (contract-call? .vault-stx set-authorized-contract .market true))
    (try! (contract-call? .vault-sbtc set-authorized-contract .market true))
    (try! (contract-call? .vault-ststx set-authorized-contract .market true))
    (try! (contract-call? .vault-usdc set-authorized-contract .market true))
    (try! (contract-call? .vault-usdh set-authorized-contract .market true))
    (try! (contract-call? .vault-ststxbtc set-authorized-contract .market true))
```
