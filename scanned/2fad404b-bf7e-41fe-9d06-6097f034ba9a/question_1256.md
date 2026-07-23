# Q1256: Entry Restriction Bypass in verify_module

## Question
Can crafted bytecode or metadata routed through `third_party/move/move-bytecode-verifier/src/limits.rs::verify_module` bypass entry-function, native-call, special-address, or module-init restrictions and reach execution paths that should never be user-callable?

## Target
- File/function: third_party/move/move-bytecode-verifier/src/limits.rs::verify_module
- Entrypoint: Publish a package or submit a script or entry-function payload whose bytecode or metadata is processed by `verify_module`.
- Attacker controls: module and package bytes, dependency graphs, identifiers, type tags, constants, metadata sections, script payloads, and serialized layouts
- Exploit idea: Exploit missing or inconsistent enforcement of entry-only, native, special-address, or module-init rules.
- Invariant to test: User-submitted code must never cross verifier or runtime boundaries into restricted entry, native, or initialization paths.
- Expected Immunefi impact: High. Unauthorized transaction execution, replay, signer confusion, signature or proof misbinding, or sequence and authorization bypass that lets an unprivileged attacker cause state transitions on behalf of another user or module context.
- Fast validation: Publish adversarial packages or scripts that try to cross each restricted boundary and assert the verifier and runtime both reject them.
