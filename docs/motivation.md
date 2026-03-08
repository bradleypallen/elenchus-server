# Motivation and Approach

This page introduces the philosophical and theoretical foundations of Elenchus. If you just want to use the system, see the [User Guide](guide.md). For implementation details, see [Architecture](architecture.md).

## The Knowledge Acquisition Problem

Knowledge engineering — the formalization of knowledge for use in information systems — has faced a persistent bottleneck since its earliest days (Feigenbaum 1977): how do you get what an expert knows into a formal system? Despite fifty years of effort across expert systems, ontology engineering, and knowledge graph construction, the problem persists.

Traditional approaches treat expert knowledge as determinate internal content awaiting extraction — as if experts have formal structures in their heads that need only be transcribed. But knowledge is not determinate prior to articulation; it is constituted through practices of expression and negotiation. Elenchus starts from a different premise: the task is not to *extract* pre-formed content but to *make explicit*, through dialogue, the inferential relationships implicit in expert practice.

## Inferentialism: Meaning as Inferential Role

Standard approaches to semantics are *representationalist*: the meaning of a sentence is given by its truth conditions. Inference is derivative — valid inferences preserve truth. *Inferentialism* (Sellars 1953; Brandom 1994) inverts this picture. The meaning of a sentence is given by its inferential role: what it follows from, what follows from it, and what it is incompatible with. Truth and reference are derivative; they are expressive devices for making explicit inferential relationships already implicit in practice.

For knowledge engineering, this inversion has a practical consequence. If meaning is inferential role, then a knowledge base is not primarily a set of sentences representing the world, but a structure capturing inferential relationships among sentences. What matters is not just what the expert asserts, but what follows from what according to the expert.

### Material Inference

Classical logic treats valid inference as formal: an argument is valid in virtue of its logical form, regardless of content. But much reasoning is *material*: the inference from "it is raining" to "the streets will be wet" is good not because of form but because of what the words mean. The meaning of "rain" is partly constituted by its inferential connections to "wet," "clouds," "umbrella."

Inferentialists take material inference as primary. Logical vocabulary — "if...then," "and," "or," "not" — is then a tool for making explicit these material relationships. To say "if it is raining then the streets will be wet" is to *endorse* the material inference explicitly.

## Knowledge Engineering as Explicitation

Drawing on inferentialist semantics, Elenchus reconceives knowledge engineering as *explicitation*: making explicit, through structured dialogue, the inferential commitments implicit in expert practice. On this view, a knowledge base is not a description of what the expert believes but a record of what has been articulated and defended through dialogue.

This is an instance of the explicitation pattern that Brandom takes as characteristic of logical vocabulary. Just as logical connectives make explicit the material inferential relationships in a base, the construction of the base itself makes explicit the pragmatic norms governing the dialogue from which the base was derived.

## The Prover-Skeptic Dialogue

Elenchus is structured as a *prover-skeptic dialogue* (Dutilh Novaes 2020), where one party defends a position while another challenges it:

- **The respondent** (you, the human expert) proposes commitments and denials about a topic — developing a *bilateral position* `[C : D]`.
- **The opponent** (the LLM) challenges and probes the respondent's position, detecting *tensions* — claims that parts of the position are jointly incoherent — and maintaining a record of commitments, denials, and material implications.

The respondent resolves tensions as they arise — by retracting a commitment or denial, refining a proposition to dissolve the conflict, or contesting the opponent's challenge. Only tensions the respondent *accepts* become material implications in the knowledge base; contested tensions are simply set aside.

This follows Dutilh Novaes's analysis of cooperation and adversariality in reasoning: the respondent seeks to develop a defensible position, while the opponent is neutral on outcome but insists on coherence, helping rather than competing.

## The LLM as a Defeasible Derivability Oracle

A natural question: can LLMs reliably identify material inferential relationships? Elenchus claims something weaker but sufficient: LLMs can identify *candidate* tensions reliably enough to serve as defeasible oracles whose judgments are subject to human override.

The key is defeasibility. The LLM proposes tensions; the respondent accepts or contests. Only accepted tensions enter the knowledge base. False positives are filtered by contestation; the human retains authority. The LLM surfaces candidate relationships the respondent might not have considered, while the respondent determines which actually hold.

The cost structure of oracle errors is asymmetric by design:

- **False positives** (spurious tensions) are filtered by contestation at the cost of a wasted dialogue turn.
- **False negatives** (missed tensions) are a more substantive concern — inferential relationships the LLM fails to surface — but the case study in Allen (2026) suggests that even a single dialogue session can structure expert knowledge into a material base whose implications correspond to documented design decisions.

This design transforms the hallucination problem from a reliability issue into a feature of the protocol: an LLM-proposed tension that does not reflect genuine incoherence is simply contested, and the contestation itself is part of the dialectical record.

## Material Bases and NMMS

Hlobil and Brandom (2025) formalize the inferentialist picture. A *material base* consists of an atomic language and a *base consequence relation* — a relation between sets of sentences capturing which positions are incoherent. Material bases are substructural: the consequence relation need not satisfy monotonicity (adding premises can defeat an inference) or transitivity (chains of good inferences need not compose). This suits defeasible, context-sensitive expert knowledge.

One condition is required: *Containment*, which says asserting and denying the same sentence is incoherent — the minimal coherence constraint. From any material base satisfying Containment, the NMMS (NonMonotonic MultiSuccedent) sequent calculus elaborates a logical vocabulary. The extension is:

- **Supraclassical** — all classically valid sequents hold
- **Conservative** — no new base-level consequences are introduced
- **Explicative** — logical vocabulary can express any base consequence relation

### From Dialogue to Material Base

Elenchus maps dialectical states to material bases. The atomic language is the union of all commitments and denials. The base consequence relation has two components:

1. **Material implications (I)** — accepted tensions from the dialogue. These are *discovered incoherences*: the respondent learned, through the dialectic, that certain combinations of commitments and denials cannot be jointly maintained.
2. **Containment (Cont)** — the background norm that asserting and denying the same sentence is incoherent. This is not discovered through the dialectic; it is a precondition of rational participation.

The resulting material base satisfies Containment by construction, and its structure exhibits the explicitation pattern: the two components make explicit, respectively, what the dialogue *produced* and what the dialogue *presupposed*.

Every material implication in the knowledge base has complete traceability — it originated as a tension proposed by the opponent and accepted by the respondent. The dialogue transcript records when the tension was raised, what position prompted it, and how the respondent resolved it. This contrasts with knowledge bases extracted from corpora, where provenance may be opaque or statistical.

## How Elenchus Differs

### From traditional knowledge acquisition

Traditional methodologies (CommonKADS (Schreiber et al. 2000), METHONTOLOGY (Fernández-López et al. 1997), NeOn (Suárez de Figueroa Baonza 2010)) assume the target is a description of the domain in a formal language, with a knowledge engineer mediating between expert and formalism. Elenchus eliminates this mediating step: the expert interacts directly with the opponent through natural language, and the formal structure is constructed as a byproduct of the dialogue.

### From LLM-based knowledge extraction

Recent work (Babaei Giglou et al. 2023; Yao et al. 2025) uses LLMs to generate triples, axioms, or class hierarchies, treating the LLM as a *source* of knowledge to be validated. The quality concern is well-documented: LLMs hallucinate and produce plausible but incorrect formalizations. Elenchus takes a fundamentally different stance — the LLM is not a source of knowledge but a dialectical partner. The respondent, not the LLM, is the epistemic authority. LLM unreliability is structurally contained by the respondent's authority over tension resolution.

### From LLM-assisted ontology engineering

Much recent work in LLM-assisted ontology engineering centers on *competency questions* (CQs) — natural language questions expressing an ontology's functional requirements (Grüninger and Fox 1995). OntoChat (Zhang et al. 2024) uses LLMs to support ontology requirements elicitation through user story creation, CQ extraction, and ontology testing. Zhao et al. (2024) extend this with participatory prompting to address the finding that domain experts struggle to prompt LLMs effectively. Koutsiana et al. (2024) report that while LLMs improve efficiency in knowledge graph construction, evaluation of LLM outputs remains the central challenge.

In all these systems, the LLM serves as a facilitator or generator: it elicits requirements, produces ontology fragments, or suggests competency questions, and the expert validates the output. The expert is treated as a *source* — of user stories, of competency questions, of validation judgments — rather than as an agent whose commitments are tested for coherence. Elenchus inverts this relationship: the LLM *challenges* the expert's commitments, and the expert's responses to those challenges constitute the knowledge base.

The formal outputs also differ. Competency questions and OWL fragments lack a characterized consequence relation, proven structural properties, or systematic traceability from output to the process that produced it. Elenchus produces material bases satisfying Containment, with proven supraclassicality, conservative extension, and explicitation properties, and complete traceability of every material implication to a specific dialogue move.

### From computational argumentation

In standard argumentation frameworks (Dung 1995; Modgil and Prakken 2014), the consequence relation (what follows from what) is fixed in advance. In Elenchus, the consequence relation is itself the output. Argumentation frameworks produce extensions (sets of acceptable arguments); Elenchus produces a material base — a substructural consequence relation that connects knowledge acquisition directly to the inferentialist program in philosophical logic.

### From multi-agent debate

Multi-agent debate frameworks (Irving et al. 2018; Li et al. 2024) are LLM-to-LLM systems aimed at answer quality. Elenchus is a human-AI system aimed at knowledge construction. In debate, the human is a judge who selects between positions; in Elenchus, the human is the *author* of the position, with authority over which inferences hold.

## References

- Allen, B. P. (2026). "Elenchus: Generating Knowledge Bases from Prover-Skeptic Dialogues." *arXiv preprint*.
- Allen, B. P. (2026). "pyNMMS." [PyPI package](https://pypi.org/project/pyNMMS/).
- Babaei Giglou, H.; D'Souza, J.; and Auer, S. (2023). "LLMs4OL: Large Language Models for Ontology Learning." In *Proceedings of the International Semantic Web Conference*.
- Brandom, R. (1994). *Making It Explicit: Reasoning, Representing, and Discursive Commitment.* Harvard University Press.
- Dung, P. M. (1995). "On the Acceptability of Arguments and Its Fundamental Role in Nonmonotonic Reasoning, Logic Programming and N-Person Games." *Artificial Intelligence* 77(2):321–357.
- Dutilh Novaes, C. (2020). *The Dialogical Roots of Deduction: Historical, Cognitive, and Philosophical Perspectives on Reasoning.* Cambridge University Press.
- Feigenbaum, E. A. (1977). "The Art of Artificial Intelligence: Themes and Case Studies of Knowledge Engineering." In *Proceedings of the Fifth International Joint Conference on Artificial Intelligence*, volume 2.
- Fernández-López, M.; Gómez-Pérez, A.; and Juristo, N. (1997). "METHONTOLOGY: From Ontological Art Towards Ontological Engineering." In *Proceedings of the AAAI-97 Spring Symposium Series on Ontological Engineering*, 33–40.
- Grüninger, M., and Fox, M. S. (1995). "The Role of Competency Questions in Enterprise Engineering." In *Benchmarking — Theory and Practice*, 22–31. Springer.
- Hlobil, U., and Brandom, R. B. (2025). *Reasons for Logic, Logic for Reasons: Pragmatics, Semantics, and Conceptual Roles.* Routledge.
- Irving, G.; Christiano, P.; and Amodei, D. (2018). "AI Safety via Debate." *arXiv preprint arXiv:1805.00899*.
- Koutsiana, E.; Walker, J.; Nwachukwu, M.; Meroño-Peñuela, A.; and Simperl, E. (2024). "Knowledge Prompting: How Knowledge Engineers Use Large Language Models." *arXiv preprint arXiv:2408.08878*.
- Li, Y.; Du, Y.; Zhang, J.; Hou, L.; Grabowski, P.; Li, Y.; and Ie, E. (2024). "Improving Multi-Agent Debate with Sparse Communication Topology." *arXiv preprint arXiv:2406.11776*.
- Modgil, S., and Prakken, H. (2014). "The ASPIC+ Framework for Structured Argumentation: A Tutorial." *Argument & Computation* 5(1):31–62.
- Schreiber, A. T.; Akkermans, H.; Anjewierden, A.; Shadbolt, N.; de Hoog, R.; Van de Velde, W.; and Wielinga, B. (2000). *Knowledge Engineering and Management: The CommonKADS Methodology.* MIT Press.
- Sellars, W. (1953). "Inference and Meaning." *Mind* 62(247):313–338.
- Suárez de Figueroa Baonza, M. del C. (2010). *NeOn Methodology for Building Ontology Networks: Specification, Scheduling and Reuse.* Ph.D. dissertation, Universidad Politécnica de Madrid.
- Yao, L.; Peng, J.; Mao, C.; and Luo, Y. (2025). "Exploring Large Language Models for Knowledge Graph Completion." In *ICASSP 2025*, 1–5. IEEE.
- Zhang, B.; Carriero, V. A.; Schreiberhuber, K.; Tsaneva, S.; González, L. S.; Kim, J.; and de Berardinis, J. (2024). "OntoChat: A Framework for Conversational Ontology Engineering Using Language Models." In *European Semantic Web Conference*, 102–121. Springer.
- Zhao, Y.; Zhang, B.; Hu, X.; Ouyang, S.; Kim, J.; Jain, N.; De Berardinis, J.; Meroño-Peñuela, A.; and Simperl, E. (2024). "Improving Ontology Requirements Engineering with OntoChat and Participatory Prompting." In *Proceedings of the AAAI Symposium Series* 4(1):253–257.
