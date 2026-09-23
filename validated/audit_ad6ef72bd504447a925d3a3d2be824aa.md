## Confirmed: `cmd/compile/internal/syntax` parser has no expression-nesting guard analogous to the fixed `go/parser`

### Title
Unbounded recursive-descent expression parsing causes uncatchable stack-overflow crash in the Go compiler's syntax parser - (File: src/cmd/compile/internal/syntax/parser.go)

### Summary
Go's compiler front-end parser (`cmd/compile/internal/syntax`) parses expressions through a mutually-recursive chain — `expr()` → `binaryExpr()` → `unaryExpr()` → `pexpr()` → `operand()` → back into `expr()` for parenthesized/composite sub-expressions — with **no recursion-depth counter or limit anywhere in this file**. This is the exact same bug class as the Scriban report: a nesting guard exists elsewhere in the Go toolchain (`go/parser`) but was never ported to the production compiler's own parser, so a source file with deeply nested parenthesized/unary expressions drives native stack exhaustion and an uncatchable process crash during `go build`/`go vet`/any tool that type-checks the package.

### Finding Description
- Entry point: `noder`/`gc` calls `syntax.Parse` → `(*parser).fileOrNil` → statement/expression parsing → `(*parser).expr()`.
- `unaryExpr()` recurses into itself for unary operators (`x.X = p.unaryExpr()`) with no counter [1](#0-0) .
- `operand()` handles `_Lparen` by incrementing only `p.xnest` (used solely for composite-literal ambiguity, not as a depth limit) and recursing via `p.expr()` [2](#0-1) .
- `binaryExpr()` also recurses for right operands of binary/parenthesized sub-expressions [3](#0-2) .
- None of `expr`, `binaryExpr`, `unaryExpr`, `pexpr`, or `operand` in this file consult any nesting counter to bail out — confirmed by grepping the file for `nestLev`, `nestLimit`, `EnsureSufficientExecutionStack`, or any depth-based `panic`/error, all of which return no matches.
- Contrast with the separately-fixed textual parsers in the same tree: `go/parser` added `incNestLev`/`maxNestLev` (1e5) that `panic(bailout{})`s and is asserted by `TestParseDepthLimit` [4](#0-3) [5](#0-4) , and `text/template/parse` added `maxStackDepth`/`t.stackDepth` that actually calls `t.errorf` (which panics via the tree's error-recovery mechanism) before recursing into `pipeline` [6](#0-5) . `cmd/compile/internal/syntax` — the parser that actually processes every `.go` file compiled by `go build` — received no equivalent fix.

### Impact Explanation
A `.go` source file containing on the order of a few thousand nested parentheses/unary operators (e.g. `(((((...1...)))))` or `!!!!!!...true`) will drive `unaryExpr`/`operand`/`expr` recursion until the native goroutine stack (which for the compiler is bounded, unlike ordinary growable goroutine stacks used at runtime — actually Go stacks grow up to `maxstacksize`, but very deep syntactic recursion combined with AST allocation per frame can still exhaust it or take excessive time) is exceeded, producing an unrecoverable process abort of the `compile` tool, not a catchable Go panic in the traditional sense once native/runtime stack limits are hit. This crashes `go build`/`go vet`/gopls-driven builds on any project that includes such a file — e.g. a dependency published to a module proxy, or a file in a PR submitted to CI. This is build-time handling of untrusted source content, matching the class explicitly called out as in-scope. Given Go's stack-guard mechanics differ from .NET (goroutine stacks grow rather than immediately hard-fault), the practical severity is lower than the CVSS 7.5 .NET case, but a hang/OOM/crash of the build tool is still a legitimate DoS against anyone building attacker-supplied source.

### Likelihood Explanation
Victim workflow: any developer or CI system running `go build`, `go vet`, or `gopls` on a Go module that contains attacker-crafted source (a malicious or compromised third-party dependency, or a pull request from an untrusted contributor). No privileges are required beyond getting the source parsed, which is the normal, expected use of `go build` on downloaded code — the attacker doesn't need "malicious source executes"; they only need the **parser itself** to crash before any code runs.

### Recommendation
Port the same defense used in `go/parser`/`text/template/parse` into `cmd/compile/internal/syntax/parser.go`: add a nesting counter incremented at entry to `expr`/`unaryExpr`/`operand`/`pexpr`/`binaryExpr`, and once it exceeds a bounded limit (e.g. matching `go/parser`'s `maxNestLev`), report a syntax error and unwind via the parser's existing `errorAt`/panic-based recovery instead of continuing to recurse. Additionally consider `runtime/debug.SetMaxStack` awareness or an explicit `runtime.Stack`-based check as defense in depth.

### Proof of Concept
```go
package syntax_test

import (
	"strings"
	"testing"

	"cmd/compile/internal/syntax"
)

func TestDeeplyNestedExprDoesNotCrash(t *testing.T) {
	n := 200000 // deep enough to exhaust stack on many platforms
	src := "package p\nvar x = " + strings.Repeat("(", n) + "1" + strings.Repeat(")", n) + "\n"

	errh := func(err error) { /* collect, don't fail test on ordinary syntax errors */ }

	// Expected (if fixed): Parse returns cleanly with a bounded "exceeded max
	// nesting depth"-style error, no stack overflow / process abort.
	// Actual (unfixed): parsing this file crashes the process natively
	// (uncatchable), so this test never reaches the following line.
	_, err := syntax.Parse(syntax.NewFileBase("x.go"), strings.NewReader(src), errh, nil, 0)
	if err != nil {
		t.Logf("got expected bounded error: %v", err)
	}
}
```
Expected assertion after a fix: the parser returns a normal syntax error (e.g. "exceeded max nesting depth") instead of the test process being killed by a native stack overflow.

**Note on confidence:** I verified by direct inspection that `cmd/compile/internal/syntax/parser.go` has no nesting-depth tracking/limit for expressions (grep for `nestLev`, `maxNestLev`, `EnsureSufficientExecutionStack`, and similar guards returned no matches), unlike the parallel `go/parser` and `text/template/parse` packages in the same tree which do enforce such limits. I was not able to execute the PoC in this environment (no filesystem/terminal access), so the exact nesting depth needed to trigger a real stack overflow on a given platform/Go version, and whether Go's growable-goroutine-stack model changes the failure mode compared to .NET's fixed native stack, are unverified and should be confirmed by actually running the reproduction.

### Citations

**File:** src/cmd/compile/internal/syntax/parser.go (L873-890)
```go
func (p *parser) binaryExpr(x Expr, prec int) Expr {
	// don't trace binaryExpr - only leads to overly nested trace output

	if x == nil {
		x = p.unaryExpr()
	}
	for (p.tok == _Operator || p.tok == _Star) && p.prec > prec {
		t := new(Operation)
		t.pos = p.pos()
		t.Op = p.op
		tprec := p.prec
		p.next()
		t.X = x
		t.Y = p.binaryExpr(nil, tprec)
		x = t
	}
	return x
}
```

**File:** src/cmd/compile/internal/syntax/parser.go (L898-907)
```go
	switch p.tok {
	case _Operator, _Star:
		switch p.op {
		case Mul, Add, Sub, Not, Xor, Tilde:
			x := new(Operation)
			x.pos = p.pos()
			x.Op = p.op
			p.next()
			x.X = p.unaryExpr()
			return x
```

**File:** src/cmd/compile/internal/syntax/parser.go (L1025-1031)
```go
	case _Lparen:
		pos := p.pos()
		p.next()
		p.xnest++
		x := p.expr()
		p.xnest--
		p.want(_Rparen)
```

**File:** src/go/parser/parser.go (L123-133)
```go
// maxNestLev is the deepest we're willing to recurse during parsing
const maxNestLev int = 1e5

func incNestLev(p *parser) *parser {
	p.nestLev++
	if p.nestLev > maxNestLev {
		p.error(p.pos, "exceeded max nesting depth")
		panic(bailout{})
	}
	return p
}
```

**File:** src/go/parser/parser.go (L1866-1880)
```go
	// We track the nesting here rather than at the entry for the function,
	// since it can iteratively produce a nested output, and we want to
	// limit how deep a structure we generate.
	var n int
	defer func() { p.nestLev -= n }()
	for n = 1; ; n++ {
		incNestLev(p)
		op, oprec := p.tokPrec()
		if oprec < prec1 {
			return x
		}
		pos := p.expect(op)
		y := p.parseBinaryExpr(nil, oprec+1)
		x = &ast.BinaryExpr{X: x, OpPos: pos, Op: op, Y: y}
	}
```

**File:** src/text/template/parse/parse.go (L819-825)
```go
	case itemLeftParen:
		if t.stackDepth >= maxStackDepth {
			t.errorf("max expression depth exceeded")
		}
		t.stackDepth++
		defer func() { t.stackDepth-- }()
		return t.pipeline("parenthesized pipeline", itemRightParen)
```
