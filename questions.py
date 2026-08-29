import json
import os

from decouple import config

# todo: if scope_files is: 500 > 50, 300 > 30 , 100 > 10
MAX_REPO = 20
# todo: the GitLab namespace/project path, for example group/project
SOURCE_REPO = 'Zest-Protocol/zest-v2-contracts'
# todo: the name of the repository
REPO_NAME = 'zest-v2-contracts'

run_number = os.environ.get('GITHUB_RUN_NUMBER', '0')


def get_cyclic_index(run_number, max_index=100):
    """Convert run number to a cyclic index between 1 and max_index"""
    return (int(run_number) - 1) % max_index + 1


def load_repository_urls():
    """Load repository URLs from repositories.json."""
    repo_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "repositories.json")
    if not os.path.exists(repo_file):
        return []

    try:
        with open(repo_file, 'r', encoding='utf-8') as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError):
        return []

    if not isinstance(data, list):
        return []

    return [url for url in data if isinstance(url, str) and url.strip()]


if run_number == "0":
    BASE_URL = f"https://deepwiki.com/{SOURCE_REPO}"
else:
    repository_urls = load_repository_urls()
    if repository_urls:
        run_index = get_cyclic_index(run_number, len(repository_urls))
        BASE_URL = repository_urls[run_index - 1]
    else:
        BASE_URL = f"https://deepwiki.com/{SOURCE_REPO}"

scope_files = [
    # =================================================================================
    # LENS: BROAD COVERAGE. The generalist sweep across every contract an unprivileged
    # user call actually executes. The other variants each take one axis; this one takes
    # the whole surface and is the default when no lens is obviously right.
    #
    # PROGRAM BOUNDARIES BAKED INTO THIS LIST:
    # * dao-multisig / dao-executor / dao-treasury are NOT here. Any impact requiring DAO
    #   compromise is out of scope, and full DAO control of the registries is intended.
    # * The registries below are in scope ONLY for the read and resolution paths that an
    #   ordinary user call runs. Their DAO-gated writes are intended design.
    # * Flashloan logic is out of scope protocol-wide. A flashloan may fund an attack;
    #   it may never be the bug.
    # =================================================================================

    # -- The hub: every unprivileged entry point ---------------------------------------
    # collateral-add, collateral-remove, supply-collateral-add, collateral-remove-redeem,
    # borrow, repay, liquidate, liquidate-multi, liquidate-redeem; oracle resolution and
    # callcode transforms; the per-block index-cache; health evaluation against egroup
    # LTVs; graduated liquidation and bad-debt socialization. AUDIT THIS FIRST.
    "mainnet/contracts/market/v0-4-market.clar",

    # -- The position ledger ------------------------------------------------------------
    # user-id registry, collateral and debt maps, and the 128-bit mask (collateral bits
    # 0-63, debt bits 64-127). The yield is accounting drift: a row that exists but is not
    # represented in the mask is invisible to every health check and to liquidation.
    "mainnet/contracts/market/v0-market-vault.clar",

    # -- Vaults: share math, index accrual, system borrow/repay, socialize-debt ---------
    # NOT the flashloan function. v0-vault-stx is the only native-STX path (.wstx and
    # `as-contract? ((with-stx amt))`); v0-vault-sbtc is the 8-decimal case; v0-vault-ststx
    # backs the CALLCODE-STSTX / CALLCODE-ZSTSTX double transform. usdc, usdh and ststxbtc
    # are byte-identical apart from constants, so findings port - do not spend batches on them.
    "mainnet/contracts/vault/v0-vault-stx.clar",
    "mainnet/contracts/vault/v0-vault-sbtc.clar",
    "mainnet/contracts/vault/v0-vault-ststx.clar",

    # -- Registry READ paths only -------------------------------------------------------
    # v0-assets: `status`, `status-multi`, `lookup`, `find`, `get-bitmap`, `mask-pos`,
    # `subset`, `uint-to-list-u64` - executed on every health check.
    # v0-egroup: `resolve`, `active`, `find-superset`, `population`, `filter-u128` - the
    # lookup that decides which LTV a live position is priced under. Assume the stored
    # configuration is correct; the bug must be in the lookup, not in the data.
    "mainnet/contracts/registry/v0-assets.clar",
    "mainnet/contracts/registry/v0-egroup.clar",
]


target_scopes = [
    "Critical. An unprivileged principal moves or encumbers a position it does not own. market.clar derives the acting account from `contract-caller`, yet only `collateral-add`, `supply-collateral-add` and `repay` assert `(is-eq contract-caller tx-sender)` - `collateral-remove`, `collateral-remove-redeem`, `borrow`, `liquidate`, `liquidate-multi` and `liquidate-redeem` do not. Every `receiver`, `collateral-receiver`, `funds-receiver` and `on-behalf-of` is an attacker-chosen delegation, and market-vault trusts a single `impl` var. Find one path where an intermediary contract or an attacker-supplied `<ft-trait>` makes `contract-caller` resolve to a principal that is not the funds owner. Impact: direct theft of user funds.",

    "Critical. An unprivileged borrower gets collateral priced too high or debt priced too low and walks away with more than it can be liquidated for. Attack `price-resolve`, `resolve-callcode`, `resolve-ztoken` (which reads `lindex` out of the market's own per-block `index-cache`, not the vault), `resolve-ststx`, `normalize-pyth` (int and expo sign handling), `check-confidence` against `max-confidence-ratio`, `oracle-timestamp-fresh` (monotonic `last-update` per feed key, and a future timestamp yielding a zero delta) and `write-feeds`. The bug must be in this code, not in what a real oracle published. Impact: protocol insolvency through uncollateralised debt.",

    "Critical. An unprivileged depositor extracts value through vault share math. `convert-to-shares-preview` and `convert-to-assets-preview` divide by `total-assets-preview` and `total-supply-preview`, which include unrealised interest and the not-yet-minted `calc-treasury-lp-preview`, while `deposit` and `redeem` mutate the `assets` var rather than the real balance, and `convert-to-shares-preview` returns u0 outright when assets exist but supply is zero. Show a share-price manipulation, a mint worth more than the underlying received, or a depositor credited zero shares for real tokens. Impact: direct theft of supplier principal.",

    "Critical. An unprivileged user makes interest accrual disagree with itself. `accrue` writes `index` and `lindex` but bumps `last-update` only when one of them changed; `next-index` and `next-liquidity-index` return stale values while the `accrue` pause state is set, which is a pass-through rather than a revert; `calc-multiplier-delta` rounds debt and liquidity in different directions; the market caches indexes per `stacks-block-time` and reuses them for every operation in that block. Show interest skipped, applied twice, or a `lindex` that inflates zToken collateral value. Impact: theft of unclaimed yield, or insolvency.",

    "Critical. An unprivileged liquidator is paid more collateral than the debt it actually repays, or seizes from a healthy position. Follow `calc-liquidation-params` -> `calc-liq-factor` -> `calc-liq-factor-exp` (integer `pow` and `sqrti`) -> `calc-liq-factor-bound` -> `process-debt-asset` -> `calc-final-liquidation-amounts` -> `scale-debt-for-liquidation`, plus `liquidate-multi` and `liquidate-redeem` which run with no `price-feeds`. Assert seized collateral USD equals repaid debt USD times (BPS + liq-penalty), never more. Do NOT frame this around disabled collateral, which is an accepted design decision. Impact: direct theft of borrower funds.",

    "Critical. An unprivileged actor forces or abuses bad-debt socialization so honest suppliers pay. market `socialize-debt-asset` calls the vault's `socialize-debt`, re-accrues, then removes the obligation; the vault reduces `lindex`, `principal-scaled`, `total-borrowed` and `assets` with four independent saturating formulas, `principal-reduction` from `borrowed / scaled-principal` and `new-lindex` from `old-total-assets`. Show a self-created dust or oracle-edge position that triggers socialization for profit, a write-down larger than the loss, or `lindex` driven to zero so every zToken collateral in that vault prices at 0. Impact: insolvency, or permanent freezing of funds.",

    "Critical. An unprivileged user obtains a position whose efficiency group grants a higher LTV than its real asset set. Attack the 128-bit mask plumbing end to end: `mask-update`, `mask-pos` and `subset` in market-vault; `mask-shift-combine` and `user-safe-mask` (which ANDs collateral against the enabled bitmap but keeps ALL debt bits); `mask-to-list-internal` and `mask-to-list-iter`; `get-position` versus `get-full-position` versus `get-liquidation-position`. Show a collateral or debt asset that a health check ignores, and borrow beyond real capacity. Impact: protocol insolvency.",

    "Critical. An unprivileged contract reenters the protocol mid-operation WITHOUT using a flashloan. Every registered asset is invoked through an `<ft-trait>` principal, and the composite entry points interleave writes across two contracts: `supply-collateral-add` transfers, deposits and pledges in three steps with health evaluated only at the end; `collateral-remove-redeem` checks health, then redeems shares whose redemption moves the very `lindex` the health check priced; `liquidate-redeem` does the same during a seizure. Show control returning to attacker code, or to a second Zest call, while an invariant is mid-flight. Impact: direct theft of user funds.",

    "Critical. THE INVISIBLE-DEBT BLIND SPOT. Every position read is filtered through the enabled bitmap: `relevant`, `iter-lookup-collateral` and `iter-lookup-debt` drop rows whose bit is clear, `get-assets` and `get-notional-evaluation` fold only over enabled assets, and `collateral-remove` decides `has-debt` from the enabled-only `get-position`, then takes a NO-DEBT branch that skips price resolution and every health check. This is NOT about liquidating disabled collateral; it is about a live debt obligation vanishing from a solvency check. Show a `debt` row that survives in the map but is invisible to `has-debt` and to `debt-total`, and withdraw all collateral while still owing. Impact: protocol insolvency.",

    "Critical. `status-multi` PAIRS THE WRONG VALUES. `(map unwrap-status ids mask)` is a TWO-LIST map in which `mask` is `uint-to-list-u64` of the enabled bitmap, so each asset id is paired positionally with one element of that expansion rather than with the whole bitmap, and `map` silently truncates to the shorter list. Trace what `collateral:` and `debt:` flags `status` therefore returns into `get-assets`, and show a position whose ids do not line up positionally receiving a flag it should not have, or being dropped from the notional fold entirely. Impact: insolvency or direct theft.",

    "Critical. THE MARKET CONTRACT HOLDS USER FUNDS MID-TRANSACTION. `supply-collateral-add` transfers the underlying to `current-contract`, deposits under an `as-contract?` scope carrying a wildcard fungible-token post condition, mints shares to the user, then re-enters `collateral-add` with a different trait principal. `collateral-remove-redeem` removes collateral to `(some current-contract)` and then redeems the SAME `amount` as shares, sending underlying to an attacker-chosen `funds-receiver`. Show a `shares-minted` versus `amount` mismatch, a `min-shares` or `min-underlying` bound that does not bind, or a way to capture the market's transient balance. Impact: direct theft of funds in motion.",

    "High. An unprivileged borrower drives the interest-rate curve into a degenerate segment. `interest-rate` unpacks the curve through `unpack-u16`, `unpack-u16-at` and `iter-unpack-u16` from two packed words, zips them with `zip` and `combine-elements`, then `resolve-interpolation-points`, `resolve-and-interpolate` and `linear-interpolate` interpolate at `calc-utilization` of available liquidity against `total-debt`. Show a utilization at or past the final point, at an equal x1/x2 pair, or above BPS when debt exceeds available assets, producing a zero or aborting rate. Impact: theft of unclaimed yield, or temporary freezing of every function that calls `accrue`.",

    "Critical. THE VAULT'S `assets` VAR DIVERGES FROM THE UNDERLYING IT HOLDS. `deposit` and `redeem` adjust `assets` directly, `system-repay` adds only `interest-paid`, `socialize-debt` subtracts a saturating `principal-reduction`, while `get-available-assets` and `ubalance` read the real balance and `redeem` gates on both `(>= current-assets inkind)` and `(>= available-assets inkind)`. Show a sequence after which `assets` overstates reality so the last suppliers cannot redeem, or understates it so a withdrawal exceeds the shares' worth. Impact: permanent freezing of funds, or insolvency.",

    "Critical. `system-repay` SPLITS ONE PAYMENT WITH THREE DISAGREEING FORMULAS. `capped-amount` is clamped to `total-debt`, `principal-reduction` comes from `calc-principal-ratio-reduction`, `principal-repaid` from `capped-amount x total-borrowed / debt`, and `interest-paid` is the remainder; `principal-scaled`, `total-borrowed` and `assets` are then written from three different quantities. Show a repay - direct or through `liquidate` - that clears more `principal-scaled` than value delivered, credits `assets` with interest never received, or zeroes `total-borrowed` while `principal-scaled` remains, so `total-assets` misreports solvency permanently. Impact: protocol insolvency.",

    "High. TREASURY LP MINTING DILUTES OR BRICKS THE VAULT. `accrue` mints zft to .dao-treasury as `reserve-inc x total-supply / (- (total-assets-preview) reserve-inc)`, and `total-supply-preview` adds that same not-yet-minted figure to the live supply that both conversion previews price against. Show a state where the subtraction underflows or the denominator is zero so `accrue` aborts and every deposit, redeem, borrow, repay and liquidation in that vault is frozen, or where more shares are minted than the reserve fee earned. Impact: permanent freezing of funds, or theft of unclaimed yield from suppliers.",

    "High. A POSITION RESOLVES TO A LOOSER EFFICIENCY GROUP THAN ITS ASSET SET. With the registry correctly configured by the DAO, the lookup itself may still be wrong: `resolve` -> `active` -> `find-superset` -> `iter-find-superset` walks `buckets` in population order and returns the FIRST superset rather than the tightest, and `population`, `filter-u128` and `iter-find` maintain that search. Show a mask whose resolved `LTV-BORROW`, `LTV-LIQ-PARTIAL` or `LTV-LIQ-FULL` exceeds what the correct group for that exact set allows, and borrow on it. Do not premise this on a bad or accidental DAO update. Impact: protocol insolvency.",

    "Critical. THE LEDGER AND ITS MASK STOP AGREEING. In market-vault `collateral-add`, `collateral-remove`, `debt-add-scaled` and `debt-remove-scaled`, the map mutation and `mask-update` are `let` bindings evaluated BEFORE `check-impl-auth`, the pause states and the amount assertions; `resolve-or-create` allocates ids through `increment`; `insert` rewrites the whole entry while `refresh` replaces `mask` and `last-update`. Show a non-zero collateral or debt row whose mask bit is clear - invisible to every health check and to liquidation - or a cleared bit for a row that still exists. Impact: insolvency, or permanent freezing of the affected collateral.",

    "High. THE ORACLE FRONT-RUNNING GUARD IS DEFEATED OR WEAPONISED. `last-borrow-block` is written only by `debt-add-scaled`, carried forward by `refresh` on every other write, and read by the same-block check behind `ERR-LIQUIDATION-BORROW-SAME-BLOCK`, alongside `is-liquidation-paused` reading `pause-liquidation` and the per-asset and `GLOBAL-LIQUIDATION-GRACE-ID` entries of `liquidation-grace-periods` against `stacks-block-time`. Show a one-unit dust borrow repeated each block that keeps an underwater position unliquidatable while it accrues, and quantify the supplier loss. Impact: protocol insolvency.",

    "High. ROUNDING IS SYSTEMATICALLY IN THE USER'S FAVOUR. Compare each paired conversion: `convert-to-scaled-debt` rounding up on borrow against `repay`'s `mul-div-down` / `mul-div-up` and `min` capping; `scale-debt-for-liquidation`'s round-down; `normalize` with per-asset `decimals` - round up for debt, round down for collateral - in `calculate-asset-notional-value` and `find-and-resolve-asset-value`; `mul-bps-down` and `div-bps-down`; `calc-utilization`. Show debt that rounds to zero, a repay credited for more scaled debt than tokens received, an 8-decimal versus 6-decimal asymmetry, or a dust-per-call extraction. Impact: theft of unclaimed yield, or permanently unrepayable dust debt freezing the collateral behind it.",

    "Critical. THE UNMODELLED COMPOSITION - what the design never considered. Each mechanism is correct alone; the break is where two meet, and no audit covered the seam. Look for: rehypothecated zToken collateral priced from the SAME vault's `lindex` that the holder can move by borrowing or repaying in the same transaction, a self-referential collateral loop; the ztoken-to-vault-id mapping falling through to the `u100` sentinel; a zToken freely transferable through the vault's `transfer` while the same shares back a market position; `supply-collateral-add` and `collateral-remove-redeem` combining a vault state change with a market health check that reads an `index-cache` primed before that change; `liquidate-multi` running N seizures against one price snapshot and one cache. Prove the composed sequence with a single transaction chain and assert the invariant each side assumed the other enforced. Impact: name it as direct theft, permanent freezing, or insolvency.",
]


scope_scan = [
]


def question_generator(target_file: str) -> str:
    """
    Generate exploit-focused audit and fuzzing questions for one Zest v2 target.

    ```
    target_file format:
    "'File Name: mainnet/contracts/market/v0-4-market.clar -> Scope: Critical. ...'"
    """

    prompt = f"""
    ```

    Generate exploit-focused security audit questions for this exact Zest Protocol v2 target:

    {target_file}

    Project focus:
    Zest v2 is a Clarity lending market on Stacks. `v0-4-market.clar` is the hub: it holds every
    user entry point (collateral-add, collateral-remove, supply-collateral-add,
    collateral-remove-redeem, borrow, repay, liquidate, liquidate-multi, liquidate-redeem),
    resolves Pyth and DIA prices with callcode transforms, caches indexes per block, evaluates
    health against efficiency-group LTVs, and runs graduated liquidation and bad-debt
    socialization. `v0-market-vault.clar` stores positions as a 128-bit mask (collateral bits
    0-63, debt bits 64-127) plus collateral and debt maps. Six vaults issue ztokens, accrue borrow
    and liquidity indexes, mint treasury LP, and serve system-borrow, system-repay and
    socialize-debt. v0-assets and v0-egroup supply asset ids, oracle config, decimals and
    per-combination risk parameters. Collateral can be plain or rehypothecated - a ztoken whose
    price is derived from its own vault's liquidity index.

    Rules:
    * Treat `File Name:` as the exact contract.
    * Treat `Scope:` as the ONLY impact to target.
    * Assume full repo context is accessible.
    * Do not ask for code or say anything is missing.
    * Use exact Clarity symbols (define-public/private/read-only names, map, data-var, constant).
    * Attacker is unprivileged only: an ordinary Stacks principal that funds a wallet, calls any
      public function, deploys its own Clarity contract, passes it as `<ft-trait>`, supplies its
      own `price-feeds` buffers, and controls amounts, receivers, `on-behalf-of` and call
      ordering within a block.
    * Attacker is NOT a DAO signer, executor, market impl, authorized contract, miner, oracle
      publisher or node operator. Ignore malicious-miner, chain-reorg, MEV-only and
      social-engineering assumptions.
    * PROGRAM EXCLUSIONS - a question landing in any of these wastes the whole batch:
      - ANY logic related to flashloans is OUT OF SCOPE. A flashloan may be used as a source of
        capital for a different attack, but never target `flashloan` itself, its fee, its
        `flashloan-permissions` / `default-flashloan-permissions` whitelist, or `in-flashloan`.
      - Liquidation of disabled collateral, and any other deliberate protocol safety design
        decision, is OUT OF SCOPE.
      - Anything requiring DAO compromise, or an accidental or incorrect registry update by the
        DAO, is OUT OF SCOPE. Full DAO control of the asset and egroup registries is intended
        design, and every egroup invariant needing global market and position knowledge is
        verified off-chain by the DAO before approval. Assume both registries are correctly
        configured, and target only the read and resolution paths an ordinary user call executes.
      - Also excluded everywhere: leaked keys or credentials, privileged addresses, external
        stablecoin depegs the attacker did not cause through a bug in this code, 51% and basic
        economic or governance attacks, Sybil attacks, centralization risk, lack of liquidity,
        incorrect data supplied by third-party oracles, best-practice notes, feature requests,
        and test or configuration files.
      - Oracle manipulation caused by a bug in THIS code remains fully in scope.
    * IN-SCOPE IMPACTS - every question must land on one and name it:
      Critical: direct theft of user funds at rest or in motion, other than unclaimed yield;
      permanent freezing of funds; protocol insolvency.
      High: theft of unclaimed yield or royalties; permanent freezing of unclaimed yield or
      royalties; temporary freezing of funds.
    * Every question must be a concrete real-world scenario an unprivileged principal can execute
      on mainnet with its own capital. No speculative unbounded-list, memory or resource-hygiene
      questions.
    * Clarity `+` `-` `*` abort on overflow and underflow; an abort is a finding only when it
      permanently or temporarily freezes a funds path - say which.
    * Generate 30 to 40 high-signal questions.
    * At least 70% must land on a Critical impact rather than a High one.
    * Every question must be testable by a Clarinet / vitest simnet test in `local-testing/tests`
      against a local fork. Never propose testing on mainnet or a public testnet.
    * Avoid generic checklist questions and repeated root causes.
    * Prefer questions that name TWO code sites and ask whether they agree: a writer and the guard
      that reads it, a preview function and the function that mutates state, a mask update and the
      health check that consumes it, a cached value and its source, a round-up and its paired
      round-down.
    * Prefer a question whose disagreement can be asserted numerically in one test - collateral
      seized equals repaid debt times penalty, shares out times share price equals assets in, sum
      of user debt equals `principal-scaled` times index, mask bits equal the set of non-zero rows.

    Known dead ends - do NOT generate questions about these:
    * Governance setting a bad LTV, cap, interest curve, penalty, staleness or fee.
    * Pyth or DIA publishing a wrong price, or an external oracle going stale on its own.
    * Any pause switch, grace period or cap being used by the DAO as designed.
    * A user harming only their own position with no third party and no protocol invariant broken.
    * Findings requiring the attacker to already be an authorized contract, market impl or signer.
    * Anything only reproducible against mock tokens or the mock oracle.

    Core invariants:
    * Authorization exactness: only the acting principal's own position is mutated, and privileged
      writes come only from the registered impl or an authorized contract.
    * Solvency: every vault's underlying plus outstanding debt covers all ztoken shares and all
      supplier claims; no position leaves the protocol under-collateralised for its egroup LTV.
    * Conversion symmetry: shares to assets to shares, and tokens to scaled debt to tokens, never
      round in the user's favour.
    * Price integrity: a resolved price reflects a fresh, confidence-checked feed and a callcode
      transform whose inputs the caller cannot move within the same transaction.
    * Liquidation fairness: collateral seized equals the debt actually repaid scaled by the
      penalty, never more, and only above the liquidation LTV.

    Each question must include:
    1. target function/method;
    2. attacker action (a concrete contract call with arguments);
    3. preconditions (funded principal, deployed contract, existing position, vault state);
    4. call sequence;
    5. invariant tested;
    6. the in-scope impact class it lands on;
    7. proof idea.

    Output only valid Python. No markdown. No explanations.

    questions = [
    "[File: {target_file}] [Function: symbol_or_method] Can an unprivileged ATTACKER_ACTION under PRECONDITIONS trigger CALL_SEQUENCE, violating INVARIANT, causing IMPACT_CLASS: SCOPE_IMPACT? Proof idea: Clarinet simnet test PARAMETERS and assert AUTHORIZATION_EXACTNESS, SOLVENCY, CONVERSION_SYMMETRY, PRICE_INTEGRITY, or LIQUIDATION_FAIRNESS.",
    ]
    """
    return prompt


def audit_format(security_question: str) -> str:
    """
    Generate a focused Zest v2 exploit-validation prompt.
    """

    prompt = f"""# SECURITY AUDIT PROMPT

## Question
{security_question}

## Rules
- Use existing repo context only. Analyze only this question and scoped impact.
- Attacker is unprivileged only: an ordinary Stacks principal that funds a wallet, calls any public function, deploys its own Clarity contract and passes it as `<ft-trait>`, supplies its own `price-feeds`, and controls amounts, receivers, `on-behalf-of` and call ordering. No DAO signer, executor, market impl, authorized contract, miner, oracle publisher or node operator access.
- Reject malicious-miner, chain-reorg, MEV-only and social-engineering paths.
- OUT OF SCOPE, reject on sight: any flashloan logic (`flashloan`, its fee, its permission whitelist, `in-flashloan`) - though a flashloan used purely as capital for a different attack is fine; liquidation of disabled collateral and other deliberate safety design decisions; anything requiring DAO compromise or an accidental or incorrect DAO registry update, since full DAO control of the asset and egroup registries is intended design and egroup invariants needing global position knowledge are verified off-chain before approval.
- Also reject: leaked keys, privileged addresses, external stablecoin depegs the attacker did not cause through a bug here, 51% / basic economic / governance attacks, Sybil, centralization risk, lack of liquidity, incorrect data supplied by third-party oracles, best-practice notes, feature requests, and test or configuration files. Oracle manipulation caused by a bug in THIS code stays in scope.
- The impact must be one of: Critical - direct theft of user funds at rest or in motion other than unclaimed yield, permanent freezing of funds, or protocol insolvency; High - theft of unclaimed yield or royalties, permanent freezing of unclaimed yield or royalties, or temporary freezing of funds.
- Reject Pyth and Wormhole internals, third-party token behaviour, `local-testing/**`, tests, mocks, deployment plans, docs, read-only aggregators, and dependency-only findings.
- Reject speculative resource-hygiene claims with no reachable mainnet scenario.

## Validate
- Trace the exact reachable path from the attacker's call (function, arguments, trait principal passed, price-feeds buffer, receiver, on-behalf-of, ordering within one block) into the affected function.
- Check whether existing `contract-caller` / `tx-sender` assertions, `check-impl-auth`, `check-caller-auth`, pause states, caps, `min-out` slippage bounds, health checks, or Clarity's own overflow and underflow aborts already stop it.
- Accept only a concrete loss, freezing, insolvency or unauthorized state change caused by this code.
- Name the in-scope impact class explicitly and justify it.
- Require exact file/function support and a reproducible Clarinet / vitest simnet PoC on a local fork.

## Output
If valid, output exactly:

### Title
[Bug statement] - ([File: file_path])

### Summary
[2-3 sentences]

### Finding Description
[Code path, root cause, attacker call arguments, exploit flow, and why existing checks fail]

### Impact Explanation
[Concrete scoped impact and the exact in-scope severity category it matches]

### Likelihood Explanation
[Preconditions, capital cost to the attacker, feasibility, repeatability]

### Recommendation
[Specific fix]

### Proof of Concept
[Clarinet simnet test plan with expected assertions, run on a local fork]

If invalid, output exactly:
#NoVulnerability found for this question.

No extra text.
"""
    return prompt


def validation_format(report: str) -> str:
    """
    Generate a strict bounty-style validation prompt for Zest v2 security claims.
    """
    prompt = f"""# VALIDATION PROMPT

## Security Claim
{report}

## Rules
- Validate only the submitted claim.
- Check SECURITY.md and Researcher.Md for scope, exclusions, and valid impact classes.
- Do not create a new vulnerability if the submitted claim is weak or invalid.
- Do not upgrade severity unless the provided evidence proves the higher impact.
- Reject anything requiring a DAO signer, executor, market impl, authorized contract, miner, oracle publisher, node operator, or leaked keys.
- OUT OF SCOPE, reject on sight: any flashloan logic (`flashloan`, its fee, its permission whitelist, `in-flashloan`) - though a flashloan used purely as capital for a different attack is fine; liquidation of disabled collateral and other deliberate safety design decisions; anything requiring DAO compromise or an accidental or incorrect DAO registry update, since full DAO control of the asset and egroup registries is intended design and egroup invariants needing global position knowledge are verified off-chain before approval.
- Also reject: leaked keys, privileged addresses, external stablecoin depegs the attacker did not cause through a bug here, 51% / basic economic / governance attacks, Sybil, centralization risk, lack of liquidity, incorrect data supplied by third-party oracles, best-practice notes, feature requests, and test or configuration files. Oracle manipulation caused by a bug in THIS code stays in scope.
- The impact must be one of: Critical - direct theft of user funds at rest or in motion other than unclaimed yield, permanent freezing of funds, or protocol insolvency; High - theft of unclaimed yield or royalties, permanent freezing of unclaimed yield or royalties, or temporary freezing of funds.
- Reject Pyth and Wormhole internals, third-party contracts, `local-testing/**`, tests, mocks, deployment plans, `.toml`, docs, read-only aggregator and dependency-only findings.
- Reject if the bug was already fixed, acknowledged, or covered by the published Clarity Alliance, Greybeard or Asymmetric audits.
- Reject any PoC that requires testing against mainnet or a public testnet; only local forks are permitted.
- A valid report must be triggerable by an ordinary Stacks principal on the currently deployed mainnet contracts.
- A PoC is mandatory for every severity. Prefer #NoVulnerability over speculative reports.

## Required Validation Checks
All must pass:
1. Exact in-scope file, function, and line/code references.
2. Clear root cause and broken security assumption.
3. Reachable exploit path: preconditions -> attacker contract call -> trigger -> bad result.
4. Existing caller assertions, impl auth, pause states, caps, slippage bounds, health checks and Clarity overflow aborts reviewed and shown insufficient.
5. Concrete in-scope impact class named, with realistic likelihood and attacker capital cost.
6. Reproducible proof: Clarinet / vitest simnet test on a local fork, or exact call steps.
7. No rejection reason from the program exclusions above.

## Silent Triage Questions
Before output, internally answer:
- Can an ordinary funded principal trigger this with its own calls or its own deployed contract, without any privileged role?
- Does the deployed Clarity code actually behave as claimed?
- Is the loss caused by this code, not by an oracle, a third-party token, a governance choice, or flashloan logic?
- Which of the listed in-scope impact classes does it land on, exactly?
- Would a program triager accept the proof?
- What exact test would prove it?

## Output
If valid, output exactly:

Audit Report

## Title
[Clear vulnerability statement] - ([File: file_path])

## Summary
[2-3 sentence summary of the bug and impact]

## Finding Description
[Exact code path, root cause, exploit flow, and why existing checks fail]

## Impact Explanation
[Concrete in-scope impact, severity rationale, and the exact category matched]

## Likelihood Explanation
[Attacker capability, preconditions, feasibility, repeatability]

## Recommendation
[Specific fix guidance]

## Proof of Concept
[Minimal reproducible steps or Clarinet simnet test plan on a local fork]

If invalid, output exactly:
#NoVulnerability found for this question.

Output only one of the two outcomes above. No extra text.
"""
    return prompt


def scan_format(report: str) -> str:
    """
    Generate a short cross-project analog scan prompt for Zest v2.
    """
    prompt = f"""# ANALOG SCAN PROMPT

## External Report
{report}

## Rules
- Use in-scope production repo context only (`mainnet/contracts/**`, excluding the dao directory). Do not ask for code or claim missing files.
- Use the external report only as a bug-class hint, not as proof.
- Keep only unprivileged-principal analogs in market entry points and health checks, oracle resolution and callcode transforms, the per-block index cache, position mask and collateral/debt accounting, egroup resolution, vault share math and interest accrual, treasury LP minting, or socialize-debt.
- OUT OF SCOPE, reject on sight: any flashloan logic (`flashloan`, its fee, its permission whitelist, `in-flashloan`) - though a flashloan used purely as capital for a different attack is fine; liquidation of disabled collateral and other deliberate safety design decisions; anything requiring DAO compromise or an accidental or incorrect DAO registry update, since full DAO control of the asset and egroup registries is intended design and egroup invariants needing global position knowledge are verified off-chain before approval.
- Also reject: leaked keys, privileged addresses, external stablecoin depegs the attacker did not cause through a bug here, 51% / basic economic / governance attacks, Sybil, centralization risk, lack of liquidity, incorrect data supplied by third-party oracles, best-practice notes, feature requests, and test or configuration files. Oracle manipulation caused by a bug in THIS code stays in scope.
- The impact must be one of: Critical - direct theft of user funds at rest or in motion other than unclaimed yield, permanent freezing of funds, or protocol insolvency; High - theft of unclaimed yield or royalties, permanent freezing of unclaimed yield or royalties, or temporary freezing of funds.
- Reject malicious-miner, chain-reorg, MEV-only, oracle-publisher, third-party token, `local-testing/**`, mock, deployment-plan, dependency-only and no-impact analogs.

## Validate
- Map the bug class to the strongest reachable Zest path from an ordinary principal's call or its own deployed contract.
- Prove root cause with exact file/function support.
- Name the in-scope impact class it lands on.

## Output (Strict)
If valid analog exists, output:

### Title
[Clear vulnerability statement] - ([File: file_path])

### Summary
### Finding Description
### Impact Explanation
### Likelihood Explanation
### Recommendation
### Proof of Concept

If not, output exactly:
#NoVulnerability found for this question.

No extra text.
"""
    return prompt
