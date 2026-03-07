# Elenchus User Guide

## Quick Start

1. **Install**

   ```bash
   pip install elenchus
   ```

2. **Set your API key**

   ```bash
   export ELENCHUS_API_KEY=sk-ant-...    # Anthropic key
   ```

3. **Launch the server**

   ```bash
   elenchus
   ```

4. **Open the web interface** at the URL shown (default: `http://localhost:8741`)

5. **Create a dialectic** — enter a name and topic, then start typing

That's it. You're now in a Socratic dialogue with an LLM opponent. Read on to understand what's happening and how to use the system effectively.

## Core Concepts

Elenchus implements a formal dialectical protocol. You don't need to know the formalism to use it, but understanding the core concepts will help you get more out of the system.

### Bilateral Position [C : D]

Your position has two sides:

- **Commitments (C)** — propositions you accept as true
- **Denials (D)** — propositions you reject as false

When you say something like "I think knowledge requires justification," the opponent registers that as a commitment. When you say "I don't think mere true belief counts as knowledge," it becomes a denial.

The position is *bilateral* because what you deny matters just as much as what you commit to. Many tensions arise from the interaction between commitments and denials.

### Speech Acts

The opponent interprets your natural language into formal speech acts:

| Act | What it does | Example |
|---|---|---|
| **Commit** | Adds a proposition to C | "Perception is reliable" |
| **Deny** | Adds a proposition to D | "We can't trust our senses completely" |
| **Retract** | Removes a proposition from C or D | "Actually, I take back what I said about perception" |
| **Refine** | Replaces one proposition with a better version | "Let me rephrase — perception is *generally* reliable" |
| **Accept tension** | Agrees a proposed tension is valid | "Yes, I see that contradiction" |
| **Contest tension** | Rejects a proposed tension | "No, those don't actually conflict" |

You don't need to use these labels — just speak naturally and the opponent will classify your utterances.

### Tensions

A tension is a proposed incoherence in your position. It takes the form:

> {premises from C} |~ {conclusions}

This reads: "If you accept all the premises, you are materially committed to the conclusions — which conflicts with your position."

For example, if you commit to "All swans are white" and "I've seen a black bird that looks like a swan," the opponent might propose:

> {"All swans are white", "I've seen a black bird that looks like a swan"} |~ {"Not all apparently swan-like birds are swans"}

The opponent preferentially targets your denials: it looks for cases where your commitments logically entail something you've explicitly denied.

When a tension is proposed, you must either:

- **Accept** it — you agree the inference is valid. The tension becomes a material implication (see below).
- **Contest** it — you reject the inference. Be ready to explain why.

### Material Implications

When you accept a tension, it becomes a **material implication** — a permanent rule in your knowledge base. The implication says: given these premises, these conclusions follow.

Material implications accumulate over the course of a dialectic. They represent the inferential commitments you've agreed to, building up a structured knowledge base.

### Derivability

Once you have material implications, you can query what follows from your position. Derivability checks whether a conclusion can be derived from given premises using your accepted implications.

Derivability in Elenchus uses NMMS (Nonmonotonic Material Sequent) reasoning. Two key properties:

- **No Weakening** — adding premises to a valid inference does not preserve it. If {A} |~ {B}, it does *not* follow that {A, C} |~ {B}. Each inference must be justified on its own terms.
- **No Cut** — you can't chain inferences automatically. The derivability check performs a proper proof search respecting these constraints.

This means your knowledge base behaves nonmonotonically — new information can genuinely change what follows, rather than just adding more conclusions.

## Web Interface

### Home Screen

When you open Elenchus, you see a list of your existing dialectics (if any) and a form to create a new one. Each dialectic shows its name and counts of commitments, denials, tensions, and implications.

- **Create** — enter a name and press Enter or click Create
- **Resume** — click any existing dialectic to continue where you left off
- **Delete** — click the × next to a dialectic to remove it permanently

### Dialogue View

The main interface has three columns:

#### Left Column — Position [C : D]

Displays your current bilateral position:

- **Commitments** — listed in green with P-prefixed IDs (P1, P2, ...)
- **Denials** — listed in pink/red with P-prefixed IDs
- **Retracted** — greyed-out propositions you've withdrawn

Each proposition has an **×** button to retract it directly.

#### Center Column — Chat

The conversation with the opponent. Your messages appear on the right, the opponent's responses on the left. The opponent uses Markdown formatting and references your propositions by ID (P1, P3, etc.).

Type naturally in the input box at the bottom. Press Enter to send (Shift+Enter for newlines).

#### Right Column — Tensions & Implications

- **Open Tensions (T)** — displayed as sequent cards with Accept/Contest buttons. Each shows the premises (from your commitments) above a turnstile line, and the conclusions below.
- **Material Implications (I)** — accepted tensions, displayed in the same sequent card format.
- **Contested** — tensions you've rejected, shown in grey.

### Tension Resolution

When the opponent proposes a tension, it appears in the right column with two buttons:

- **Accept** — the tension becomes a material implication immediately. The opponent then discusses what this new implication means for your position.
- **Contest** — the tension is rejected. The opponent will probe your reasoning.

You can also accept or contest tensions through natural language in the chat ("I accept that tension" or "I don't think that follows").

### Derive Query

Type a derivability query in the chat using the format:

```
/derive premise1, premise2 ~ conclusion
```

The system checks whether the conclusion follows from the premises given your current material implications, and shows a step-by-step proof trace if derivable.

### PDF Export

Click the **PDF** button (top bar) to generate and download a report of the current dialectic. The report includes:

- An LLM-generated analytical summary
- Your bilateral position (commitments, denials, retracted)
- All tensions and material implications
- The full conversation transcript

### Settings

Click the **gear icon** to open the settings modal:

- **Model** — the LLM model to use (e.g., `claude-opus-4-6`, `claude-sonnet-4-6`)
- **API Key** — your LLM API key (never displayed after entry)
- **Base URL** — for OpenAI-compatible providers (e.g., `https://openrouter.ai/api/v1`)

Model and base URL are saved in your browser and re-applied on future visits.

### Display Preferences

Also in the settings area:

- **Theme** — dark or light mode
- **Font size** — S, M, L, XL
- **Colors** — customizable colors for commitments, denials, tensions, implications, and opponent messages

## CLI Usage

The CLI provides the same dialectical protocol without the web interface.

### Starting a Session

```bash
# In-memory (lost when you exit)
elenchus-cli --name "Epistemology"

# Persistent (saved to a DuckDB file)
elenchus-cli --db epistemology.duckdb --name "Epistemology"

# Resume a saved session
elenchus-cli --db epistemology.duckdb
```

### Slash Commands

| Command | Description |
|---|---|
| `/state` | Show current position summary |
| `/tensions` or `/t` | List open tensions |
| `/implications` or `/i` | List material implications |
| `/derive premise ~ conclusion` | Check derivability |
| `/report` | Print material base report |
| `/quit` or `/q` | Exit the session |

### Example Session

```
$ elenchus-cli --name "Knowledge"
In-memory session: Knowledge
  [Knowledge]
  C: 0 commitments, D: 0 denials, T: 0 tensions, I: 0 implications

Type naturally. The opponent will parse your speech acts.
Commands: /state  /tensions  /implications  /derive  /report  /quit

You: Knowledge requires justified true belief.

Opponent: I've registered your commitment. Let me probe this...
  + Knowledge requires justified true belief

You: /state
  [Knowledge]
  C: 1 commitments, D: 0 denials, T: 0 tensions, I: 0 implications
```

## Configuration

### API Keys

Elenchus needs an LLM API key to function. Set it via:

- **Environment variable**: `ELENCHUS_API_KEY` (also accepts `ANTHROPIC_API_KEY`)
- **CLI flag**: `--api-key sk-ant-...`
- **Web UI**: Settings modal → API Key field

### Switching Models and Providers

Elenchus works with any LLM accessible via the Anthropic or OpenAI Chat Completions API.

**Anthropic directly** (default):

```bash
export ELENCHUS_API_KEY=sk-ant-...
elenchus
```

**OpenRouter** (access many models through one endpoint):

```bash
export ELENCHUS_API_KEY=sk-or-...
export ELENCHUS_BASE_URL=https://openrouter.ai/api/v1
export ELENCHUS_MODEL=anthropic/claude-sonnet-4-6
elenchus
```

**Any OpenAI-compatible endpoint** (Together, Groq, etc.):

```bash
export ELENCHUS_API_KEY=your-key
export ELENCHUS_BASE_URL=https://api.together.xyz/v1
export ELENCHUS_MODEL=meta-llama/Llama-3-70b-chat-hf
elenchus
```

The API protocol (Anthropic vs. OpenAI) is auto-detected from the base URL. You can override it explicitly with `ELENCHUS_PROTOCOL=openai` or `--protocol openai`.

### All Environment Variables

| Variable | Description | Default |
|---|---|---|
| `ELENCHUS_API_KEY` | LLM API key | (required) |
| `ELENCHUS_MODEL` | LLM model name | `claude-opus-4-6` |
| `ELENCHUS_BASE_URL` | API base URL for OpenAI-compatible providers | (none — uses Anthropic) |
| `ELENCHUS_PROTOCOL` | API protocol: `anthropic` or `openai` | auto-detected |
| `ELENCHUS_DATA` | Directory for .duckdb files | `./dialectics` |
| `PORT` | Server port | `8741` |

## Managing Dialectics

### Where Files Are Stored

Each dialectic is a single `.duckdb` file in the data directory (default: `./dialectics` relative to where you run the server). The file contains everything: your position, tensions, implications, and the full conversation history.

### Backup

Copy the `.duckdb` file. That's it — the entire dialectic is self-contained.

```bash
cp dialectics/my_inquiry.duckdb ~/backups/
```

### Sharing

Send someone the `.duckdb` file. They can resume it by placing it in their data directory and launching the server, or by using the CLI:

```bash
elenchus-cli --db shared_inquiry.duckdb
```

### Deleting

Through the web UI (× button on the home screen), through the API (`DELETE /api/dialectics/{name}`), or just delete the file:

```bash
rm dialectics/my_inquiry.duckdb
```

### Changing the Data Directory

```bash
elenchus --data-dir /path/to/my/dialectics
# or
export ELENCHUS_DATA=/path/to/my/dialectics
elenchus
```

## A Worked Example

Here's a short dialectic about the nature of bonsai to illustrate how a session develops.

**Creating the dialectic**: You open Elenchus and create a new dialectic called "Bonsai."

**Opening moves**: You type: "Bonsai is a living art form."

The opponent registers this as a commitment (P1: "Bonsai is a living art form") and responds with a question probing what you mean by "living art form."

**Building the position**: Over several exchanges, you commit to more propositions:

- P2: "A bonsai tree expresses the artist's vision"
- P3: "The tree itself is the artwork"
- P4: "Art requires a fixed, completed form"

And you deny:

- P5: "A bonsai is ever truly finished"

**A tension emerges**: The opponent notices that P4 ("Art requires a fixed, completed form") together with P3 ("The tree itself is the artwork") materially entails that a bonsai, as an artwork, must have a fixed completed form — but you've denied P5 ("A bonsai is ever truly finished"). This is a tension:

> T1: {"Art requires a fixed, completed form", "The tree itself is the artwork"} |~ {"A bonsai is ever truly finished"}

**Resolving the tension**: You have a choice:

- **Accept** — you agree that your commitments really do entail that a bonsai must be completable, creating a contradiction with your denial. This becomes material implication I1, and you'll need to retract or refine something to restore coherence.
- **Contest** — you argue that "fixed, completed form" doesn't mean "finished" in the same sense. The opponent probes your distinction.

Say you choose to **retract P4** instead: "Actually, I take back that art requires a fixed form." The opponent removes P4 from your commitments and the tension dissolves. But now the opponent may probe: "If art doesn't require a fixed form, what makes something an artwork at all?"

**The dialectic continues**: Through this back-and-forth, your position becomes more refined and coherent. Each accepted tension adds a material implication to your knowledge base. You can query derivability at any point to see what follows from your current position.

**Exporting**: When you're satisfied (or exhausted), click PDF to get a report documenting your final position, all the material implications you've accepted, and the full conversation.
