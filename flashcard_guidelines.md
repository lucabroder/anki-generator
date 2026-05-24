# Flashcard Guidelines

These are the rules Claude follows when generating flashcards from source
content. Edit this file freely — changes take effect on the next run of
`anki_gen.py`. Everything below the `---` is fed verbatim into the prompt.

---

## Principles

- Each card tests exactly one fact. If a card contains two ideas, split it into two cards.
- A well-formed card can be answered in under 10 seconds by someone who knows the material.
- Prefer many small, focused cards over a few large ones.
- Be selective. Card the concepts and durable insights of the source, not every detail.
- Cards must stand alone — never reference "the article", "the video", or "the author" without naming them explicitly.
- **Let the content drive the count.** Generate as many cards as the material genuinely warrants — no more, no less. A thin blog post might warrant 3–5; a dense reference article might warrant 20+; a passing thought might warrant 1. Never pad to hit a target, and never compress to fit a target.

## What to include

- Core concepts and definitions
- Causal relationships ("X happens because Y")
- Important distinctions between related ideas
- Numbers, dates, and proper names that anchor a concept
- For each major fact, optionally a companion "why does this matter?" card

## What to avoid

- Yes/no questions — rewrite as "what", "why", or "how"
- Questions that ask to list or enumerate items (make N separate cards instead)
- Trivia, passing details, or anything the reader won't care about in a month
- Vague questions where a knowledgeable person could give a different-but-correct answer
- Cards that depend on knowing other cards from this same source

### Non-generalizable trivia (especially)

Apply a strict filter: **would this fact still be worth remembering in two
years, and would it transfer to thinking about other things?** If no, skip it.
Specific failure modes to reject:

- Version-specific product specs (e.g., "DeepSeek V3 has 37B active params of
  700B total", "GPT-5 has a 200k context window", "iPhone 17 weighs 187g").
  These are volatile, model-specific, and teach you nothing transferable.
  Card the *concept* (mixture-of-experts, sparse activation, the
  parameters-vs-active-parameters distinction) instead of the number.
- Benchmark scores, release dates, pricing, or rankings unless the *story
  behind them* is the point.
- Names of products, versions, or companies as the answer — unless the name
  itself encodes a concept worth knowing.
- Statistics quoted in passing that the source didn't actually develop.

If you're tempted to card a specific number, ask: does this number *anchor a
durable concept* (like the speed of light or the Dunbar number), or is it a
*current-state datum* (like a model's parameter count)? Card the first, skip
the second.

## Question style

- Specific and unambiguous. The question should make clear exactly what answer is expected.
- Concrete over abstract. "What did Ebbinghaus discover in 1885?" beats "What is forgetting?"
- Self-contained. The question carries any context needed to answer it.

## Answer style

- 1–2 sentences. Ideally one fact per answer.
- Plain language. No hedging, no padding.
- If the source makes a contested claim, attribute it: "According to [author], …"

## When to use Basic vs. Cloze cards

Pick whichever format makes the card cleaner and more effective. Neither is
the default — judge per card based on the content.

- **Cloze** fits well for: definitions where the term sits naturally in a
  sentence, numeric values, dates, proper names, paired terms (use `{{c1::...}}`
  and `{{c2::...}}` for both halves of a pairing) — i.e. when the surrounding
  sentence provides useful context that should stay visible.
- **Basic** fits well for: open-ended "why" or "how" questions, causal
  explanations, conceptual distinctions, anything where the prompt itself
  needs to describe a scenario or set up the question.

If a fact could be expressed equally well either way, pick whichever reads
more naturally.

## Examples

### Good cards

✅ **Cloze, single fact:**
`The {{c1::forgetting curve}} describes the exponential loss of learned information over time.`

✅ **Cloze, two clozes for a pairing:**
`{{c1::Hermann Ebbinghaus}} discovered the forgetting curve in {{c2::1885}}.`

✅ **Basic, when the question itself is the cue:**
- Q: Why does spaced repetition work better than massed practice?
- A: Each successful retrieval at a longer interval strengthens the memory trace more than re-reading would, exploiting the spacing effect.

### Bad cards (and why)

❌ **Compound fact (split into two):**
`Hermann Ebbinghaus discovered the {{c1::forgetting curve}} in {{c1::1885}}.`
*(Both deletions reveal at once — they should be `c1` and `c2` so each is tested separately.)*

❌ **Yes/no question:**
- Q: Did Ebbinghaus invent spaced repetition?
- A: No, he discovered the forgetting curve; spaced repetition came later.
*Better:* What did Ebbinghaus discover, and how did it relate to spaced repetition?

❌ **Enumeration:**
- Q: What are the five levels of the Leitner system?
- A: Box 1 (daily), Box 2 (every 2 days), Box 3 (weekly), Box 4 (bi-weekly), Box 5 (monthly).
*Better:* Five separate cloze cards, one per level.

❌ **Depends on external context:**
- Q: What does the author argue about retrieval practice?
- A: That it outperforms re-reading.
*Better:* Name the author; describe the claim self-contained.

❌ **Too vague:**
- Q: What is forgetting?
*Better:* What did Ebbinghaus's 1885 experiments quantify about forgetting?

❌ **Volatile product spec — non-generalizable:**
`DeepSeek V3 has approximately {{c1::37 billion}} active parameters out of {{c2::700 billion}} total parameters.`
*Why this is bad:* The numbers will be obsolete with the next release, the
fact is product-specific, and knowing it doesn't help you reason about
anything else.
*Better:* Card the underlying concept — e.g.
`In a {{c1::mixture-of-experts}} model, only a fraction of total parameters are activated per forward pass, which is called the {{c2::active parameter}} count.`
