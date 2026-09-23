## Finding

Based on the investigation, the closest production Go analog to the CVE-2023-29583 stack-overflow bug class (unbounded recursive-descent expression parsing without depth limiting) is `cmd/compile/internal/syntax/parser.go`'s expression grammar, which lacks the recursion-depth guard present in its sibling packages.

### Title
Unbounded recursion in `cmd/compile/internal/syntax` expression parser causes stack-overflow crash on deeply nested source - (File: `src/cmd/compile/internal/syntax/parser.go`)

### Summary
The Go compiler's own source parser (`cmd/compile/internal/syntax`) parses expressions via mutually recursive `binaryExpr`/`unaryExpr` functions with no bound on recursion depth [1](#0-0) [2](#0-1) . In contrast, the sibling `go/parser` package explicitly added a nesting-depth counter (`nestLev`, `maxNestLev = 1e5`) specifically to prevent stack-exhaustion crashes from pathological input [3](#0-2) [4](#0-3) , and is applied inside `parseUnaryExpr`/`parseBinaryExpr` [5](#0-4) . `text/template/parse` similarly enforces a `maxStackDepth` for nested expressions [6](#0-5) . `cmd/compile/internal/syntax/parser.go` has no equivalent (`nestLev`/`maxNestLev` do not appear in that file).

### Finding Description
A source file containing a sufficiently deeply nested expression (e.g. thousands of nested unary operators `+++++...1` or a long left-associative chain feeding `binaryExpr`'s recursive `p.binaryExpr(nil, tprec)` call at line 886, or `unaryExpr`'s self-recursive call to `p.unaryExpr()` at lines 906/916) is fed to `go build`/`go vet`/`gofmt`-style tooling that uses `cmd/compile/internal/syntax` (the compiler front end). Each nesting level adds a stack frame with no bound check, unlike the parallel `go/parser` path, so sufficiently deep nesting exhausts the goroutine stack and triggers a fatal, unrecoverable `runtime: goroutine stack exceeds ... -memory` crash of the compiling process.

### Impact Explanation
This crashes the `go build` toolchain process itself (denial of service on a compilation host, CI system, Go Playground-style sandbox, or any tool that compiles third-party/untrusted `.go` source, such as a malicious module fetched via `go get`). Because it is a stack overflow, `recover()` cannot catch it (it is a fatal runtime error, not a normal panic), making it more severe than a typical parser panic.

### Likelihood Explanation
Any victim workflow that compiles source it did not author — e.g., building a downloaded/third-party Go module, a CI pipeline compiling PR-submitted code, or an online sandbox/playground compiling user-submitted snippets — would trigger this by simply including a deeply/pathologically nested expression in a `.go` file. No special privileges are required; the attacker only needs to supply the source text that gets compiled.

### Recommendation
Port the same mitigation used in `go/parser` (`incNestLev`/`decNestLev`/`maxNestLev`) or `text/template/parse` (`maxStackDepth`) into `cmd/compile/internal/syntax/parser.go`'s `binaryExpr` and `unaryExpr`, converting stack overflow into a bounded, recoverable syntax error once a nesting threshold is exceeded.

### Proof of Concept
```go
package main

import (
	"strings"
	"testing"

	"cmd/compile/internal/syntax"
)

func TestDeeplyNestedUnaryExprStackOverflow(t *testing.T) {
	// Build a Go source file with a pathologically deep unary-expression chain.
	src := "package p\nvar x = " + strings.Repeat("+", 5_000_000) + "1\n"
	errh := func(err error) { t.Log(err) }
	// Expected (buggy) behavior: process crashes with
	// "runtime: goroutine stack exceeds ... -memory" (fatal, unrecoverable),
	// instead of returning a bounded syntax error like go/parser does
	// for the equivalent nestLev-guarded case.
	_, _ = syntax.Parse(syntax.NewFileBase("x.go"), strings.NewReader(src), errh, nil, 0)
}
```
Running this against the current `cmd/compile/internal/syntax` (no `nestLev` guard) is expected to crash the process via stack exhaustion rather than return a syntax error, whereas the equivalent `go/parser.ParseExpr` call on the same input returns a bounded `"exceeded max nesting depth"` error due to its `maxNestLev` check. [7](#0-6) [8](#0-7) [6](#0-5)

### Citations

**File:** src/cmd/compile/internal/syntax/parser.go (L872-918)
```go
// Expression = UnaryExpr | Expression binary_op Expression .
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

// UnaryExpr = PrimaryExpr | unary_op UnaryExpr .
func (p *parser) unaryExpr() Expr {
	if trace {
		defer p.trace("unaryExpr")()
	}

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

		case And:
			x := new(Operation)
			x.pos = p.pos()
			x.Op = And
			p.next()
			// unaryExpr may have returned a parenthesized composite literal
			// (see comment in operand) - remove parentheses if any
			x.X = Unparen(p.unaryExpr())
			return x
		}
```

**File:** src/go/parser/parser.go (L72-74)
```go
	// nestLev is used to track and limit the recursion depth
	// during parsing.
	nestLev int
```

**File:** src/go/parser/parser.go (L123-139)
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

// decNestLev is used to track nesting depth during parsing to prevent stack exhaustion.
// It is used along with incNestLev in a similar fashion to how un and trace are used.
func decNestLev(p *parser) {
	p.nestLev--
}
```

**File:** src/go/parser/parser.go (L1775-1881)
```go
func (p *parser) parseUnaryExpr() ast.Expr {
	defer decNestLev(incNestLev(p))

	if p.trace {
		defer un(trace(p, "UnaryExpr"))
	}

	switch p.tok {
	case token.ADD, token.SUB, token.NOT, token.XOR, token.AND, token.TILDE:
		pos, op := p.pos, p.tok
		p.next()
		x := p.parseUnaryExpr()
		return &ast.UnaryExpr{OpPos: pos, Op: op, X: x}

	case token.ARROW:
		// channel type or receive expression
		arrow := p.pos
		p.next()

		// If the next token is token.CHAN we still don't know if it
		// is a channel type or a receive operation - we only know
		// once we have found the end of the unary expression. There
		// are two cases:
		//
		//   <- type  => (<-type) must be channel type
		//   <- expr  => <-(expr) is a receive from an expression
		//
		// In the first case, the arrow must be re-associated with
		// the channel type parsed already:
		//
		//   <- (chan type)    =>  (<-chan type)
		//   <- (chan<- type)  =>  (<-chan (<-type))

		x := p.parseUnaryExpr()

		// determine which case we have
		if typ, ok := x.(*ast.ChanType); ok {
			// (<-type)

			// re-associate position info and <-
			dir := ast.SEND
			for ok && dir == ast.SEND {
				if typ.Dir == ast.RECV {
					// error: (<-type) is (<-(<-chan T))
					p.errorExpected(typ.Arrow, "'chan'")
				}
				arrow, typ.Begin, typ.Arrow = typ.Arrow, arrow, arrow
				dir, typ.Dir = typ.Dir, ast.RECV
				typ, ok = typ.Value.(*ast.ChanType)
			}
			if dir == ast.SEND {
				p.errorExpected(arrow, "channel type")
			}

			return x
		}

		// <-(expr)
		return &ast.UnaryExpr{OpPos: arrow, Op: token.ARROW, X: x}

	case token.MUL:
		// pointer type or unary "*" expression
		pos := p.pos
		p.next()
		x := p.parseUnaryExpr()
		return &ast.StarExpr{Star: pos, X: x}
	}

	return p.parsePrimaryExpr(nil)
}

func (p *parser) tokPrec() (token.Token, int) {
	tok := p.tok
	if p.inRhs && tok == token.ASSIGN {
		tok = token.EQL
	}
	return tok, tok.Precedence()
}

// parseBinaryExpr parses a (possibly) binary expression.
// If x is non-nil, it is used as the left operand.
//
// TODO(rfindley): parseBinaryExpr has become overloaded. Consider refactoring.
func (p *parser) parseBinaryExpr(x ast.Expr, prec1 int) ast.Expr {
	if p.trace {
		defer un(trace(p, "BinaryExpr"))
	}

	if x == nil {
		x = p.parseUnaryExpr()
	}
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
}
```

**File:** src/text/template/parse/parse.go (L49-57)
```go
// maxStackDepth is the maximum depth permitted for nested
// parenthesized expressions.
var maxStackDepth = 10000

// init reduces maxStackDepth for WebAssembly due to its smaller stack size.
func init() {
	if runtime.GOARCH == "wasm" {
		maxStackDepth = 1000
	}
```
