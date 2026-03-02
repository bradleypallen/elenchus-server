"""
pdf_report.py — PDF report generation for Elenchus dialectics

Generates a structured PDF covering the full dialectical state:
summary, bilateral position, tensions, material implications,
material base report, and conversation transcript.
"""

import json
import logging
import os
from datetime import datetime

from fpdf import FPDF

from dialectical_state import DialecticalState
from material_base import str_to_set, fmt_set

logger = logging.getLogger(__name__)

# ── Font discovery ──

# Preferred fonts in order. We need a proportional font with Unicode
# support and a monospace font for sequent notation.
_FONT_CANDIDATES_BODY = [
    '/Library/Fonts/Arial Unicode.ttf',           # macOS
    '/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf',  # Debian/Ubuntu
    '/usr/share/fonts/TTF/DejaVuSans.ttf',         # Arch
]

_FONT_CANDIDATES_MONO = [
    '/System/Library/Fonts/SFNSMono.ttf',          # macOS
    '/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf',  # Debian/Ubuntu
    '/usr/share/fonts/TTF/DejaVuSansMono.ttf',     # Arch
]


def _find_font(candidates: list[str]) -> str | None:
    for path in candidates:
        if os.path.exists(path):
            return path
    return None


def _parse_assistant_content(content: str) -> str:
    """Extract the 'response' field from raw LLM JSON output.

    The opponent stores raw JSON in the conversation table. This
    strips any markdown code fence, parses the JSON, and returns
    the natural language response field.
    """
    try:
        clean = content.strip()
        # Strip markdown code fence if present
        if clean.startswith('```'):
            clean = clean.split('\n', 1)[1]
            if clean.endswith('```'):
                clean = clean[:-3]
            clean = clean.strip()
        parsed = json.loads(clean)
        return parsed.get('response', content)
    except (json.JSONDecodeError, IndexError, AttributeError):
        return content


def generate_pdf_report(state: DialecticalState, summary: str) -> bytes:
    """Build a PDF report of the dialectic state.

    Args:
        state: The current DialecticalState
        summary: LLM-generated analytical summary text

    Returns:
        PDF file contents as bytes
    """
    s = state.to_dict()
    logger.info("Generating PDF report for dialectic '%s'", s['name'])

    pdf = FPDF()
    pdf.set_auto_page_break(auto=True, margin=20)
    pdf.set_margins(20, 20, 20)

    # ── Font setup ──
    body_font_path = _find_font(_FONT_CANDIDATES_BODY)
    mono_font_path = _find_font(_FONT_CANDIDATES_MONO)

    use_unicode = False
    if body_font_path:
        pdf.add_font('Body', '', body_font_path)
        pdf.add_font('Body', 'B', body_font_path)  # fake bold via same file
        use_unicode = True
        logger.info("PDF using Unicode body font: %s", body_font_path)
    if mono_font_path:
        pdf.add_font('Mono', '', mono_font_path)
        logger.info("PDF using Unicode mono font: %s", mono_font_path)

    body_family = 'Body' if use_unicode else 'Helvetica'
    mono_family = 'Mono' if mono_font_path else 'Courier'

    # ── Helper functions ──

    def set_body(size=10, style=''):
        pdf.set_font(body_family, style, size)

    def set_mono(size=9):
        pdf.set_font(mono_family, '', size)

    def set_heading(size=14):
        pdf.set_font(body_family, 'B', size)

    def section_title(num, title):
        pdf.ln(6)
        set_heading(13)
        pdf.set_text_color(50, 50, 80)
        pdf.cell(text=f"{num}. {title}")
        pdf.ln(7)
        # Thin rule
        pdf.set_draw_color(180, 180, 200)
        pdf.line(20, pdf.get_y(), pdf.w - 20, pdf.get_y())
        pdf.ln(4)
        pdf.set_text_color(0, 0, 0)

    def bullet(text, indent=6):
        set_body(10)
        x = pdf.get_x()
        pdf.set_x(x + indent)
        pdf.cell(text="\u2022 ", w=6)
        pdf.multi_cell(w=pdf.w - pdf.get_x() - 20, text=text)
        pdf.ln(1)

    def sequent_line(gamma_list, delta_list, prefix='', indent=6):
        """Render a sequent {gamma} |~ {delta} in monospace."""
        g = ', '.join(gamma_list)
        d = ', '.join(delta_list)
        # Use ASCII-safe turnstile representation
        line = f"{prefix}{{{g}}} |~ {{{d}}}"
        set_mono(9)
        x = pdf.get_x()
        pdf.set_x(x + indent)
        pdf.multi_cell(w=pdf.w - pdf.get_x() - 20, text=line)
        pdf.ln(1)

    # ── Page 1: Title ──

    pdf.add_page()
    pdf.ln(20)

    set_heading(22)
    pdf.set_text_color(50, 50, 80)
    pdf.cell(text="ELENCHUS DIALECTIC REPORT", align='C', center=True)
    pdf.ln(12)

    set_heading(16)
    pdf.set_text_color(80, 80, 100)
    pdf.cell(text=s['name'], align='C', center=True)
    pdf.ln(10)

    set_body(10)
    pdf.set_text_color(120, 120, 140)
    pdf.cell(
        text=f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M')}",
        align='C', center=True,
    )
    pdf.ln(8)

    # Stats line
    stats = (f"C:{len(s['commitments'])}  D:{len(s['denials'])}  "
             f"T:{len(s['tensions'])}  I:{len(s['implications'])}")
    pdf.cell(text=stats, align='C', center=True)
    pdf.ln(16)

    pdf.set_text_color(0, 0, 0)

    # ── Section 1: Summary ──

    section_title(1, "SUMMARY")
    set_body(10)
    pdf.multi_cell(w=0, text=summary)
    pdf.ln(4)

    # ── Section 2: Bilateral Position [C : D] ──

    section_title(2, "BILATERAL POSITION [C : D]")

    # 2.1 Commitments
    set_body(11, 'B')
    pdf.cell(text=f"2.1 Commitments ({len(s['commitments'])})")
    pdf.ln(5)
    if s['commitments']:
        for c in s['commitments']:
            bullet(c)
    else:
        set_body(10)
        pdf.set_x(pdf.get_x() + 6)
        pdf.set_text_color(120, 120, 140)
        pdf.cell(text="(none)")
        pdf.set_text_color(0, 0, 0)
        pdf.ln(5)
    pdf.ln(3)

    # 2.2 Denials
    set_body(11, 'B')
    pdf.cell(text=f"2.2 Denials ({len(s['denials'])})")
    pdf.ln(5)
    if s['denials']:
        for d in s['denials']:
            bullet(d)
    else:
        set_body(10)
        pdf.set_x(pdf.get_x() + 6)
        pdf.set_text_color(120, 120, 140)
        pdf.cell(text="(none)")
        pdf.set_text_color(0, 0, 0)
        pdf.ln(5)
    pdf.ln(3)

    # 2.3 Retracted
    retracted = s.get('retracted', [])
    set_body(11, 'B')
    pdf.cell(text=f"2.3 Retracted ({len(retracted)})")
    pdf.ln(5)
    if retracted:
        for r in retracted:
            bullet(r)
    else:
        set_body(10)
        pdf.set_x(pdf.get_x() + 6)
        pdf.set_text_color(120, 120, 140)
        pdf.cell(text="(none)")
        pdf.set_text_color(0, 0, 0)
        pdf.ln(5)
    pdf.ln(3)

    # ── Section 3: Tensions ──

    section_title(3, "TENSIONS")

    # 3.1 Open
    open_tensions = s['tensions']
    set_body(11, 'B')
    pdf.cell(text=f"3.1 Open ({len(open_tensions)})")
    pdf.ln(5)
    if open_tensions:
        for t in open_tensions:
            set_body(10)
            pdf.set_x(pdf.get_x() + 6)
            pdf.cell(text=f"#{t['id']}: ")
            pdf.ln(4)
            sequent_line(t['gamma'], t['delta'], indent=12)
            if t.get('reason'):
                set_body(9)
                pdf.set_x(pdf.get_x() + 12)
                pdf.set_text_color(100, 100, 120)
                pdf.multi_cell(w=pdf.w - pdf.get_x() - 20,
                               text=f"Reason: {t['reason']}")
                pdf.set_text_color(0, 0, 0)
            pdf.ln(2)
    else:
        set_body(10)
        pdf.set_x(pdf.get_x() + 6)
        pdf.set_text_color(120, 120, 140)
        pdf.cell(text="(none)")
        pdf.set_text_color(0, 0, 0)
        pdf.ln(5)
    pdf.ln(3)

    # 3.2 Contested
    contested = s.get('contested', [])
    set_body(11, 'B')
    pdf.cell(text=f"3.2 Contested ({len(contested)})")
    pdf.ln(5)
    if contested:
        for t in contested:
            set_body(10)
            pdf.set_x(pdf.get_x() + 6)
            pdf.cell(text=f"#{t['id']}: ")
            pdf.ln(4)
            sequent_line(t['gamma'], t['delta'], indent=12)
            if t.get('reason'):
                set_body(9)
                pdf.set_x(pdf.get_x() + 12)
                pdf.set_text_color(100, 100, 120)
                pdf.multi_cell(w=pdf.w - pdf.get_x() - 20,
                               text=f"Reason: {t['reason']}")
                pdf.set_text_color(0, 0, 0)
            pdf.ln(2)
    else:
        set_body(10)
        pdf.set_x(pdf.get_x() + 6)
        pdf.set_text_color(120, 120, 140)
        pdf.cell(text="(none)")
        pdf.set_text_color(0, 0, 0)
        pdf.ln(5)
    pdf.ln(3)

    # ── Section 4: Material Implications ──

    implications = s['implications']
    section_title(4, f"MATERIAL IMPLICATIONS ({len(implications)})")

    if implications:
        for imp in implications:
            sequent_line(imp['gamma'], imp['delta'])
    else:
        set_body(10)
        pdf.set_text_color(120, 120, 140)
        pdf.cell(text="(none)")
        pdf.set_text_color(0, 0, 0)
        pdf.ln(5)
    pdf.ln(3)

    # ── Section 5: Material Base ──

    section_title(5, "MATERIAL BASE")

    # Atoms and sequents from the base
    atoms = state.base.atoms
    base_rows = state.base.con.execute(
        "SELECT premises, conclusions FROM base_sequents"
    ).fetchall()
    completeness = state.base.completeness()

    set_body(10)
    pdf.cell(text=f"Atoms: {len(atoms)}  |  Sequents: {len(base_rows)}")
    pdf.ln(5)
    pdf.cell(
        text=f"Completeness: {completeness['pct']:.0%} "
             f"({completeness['assessed']}/{completeness['total']})"
    )
    pdf.ln(6)

    if base_rows:
        for bp, bc in base_rows:
            p_set = list(str_to_set(bp))
            c_set = list(str_to_set(bc))
            sequent_line(p_set, c_set)
    pdf.ln(3)

    # ── Section 6: Conversation Transcript ──

    section_title(6, "CONVERSATION TRANSCRIPT")

    conversation = state.get_conversation()
    if conversation:
        for msg in conversation:
            role = msg['role'].upper()
            content = msg['content']

            # Parse assistant messages to extract natural language
            if msg['role'] == 'assistant':
                content = _parse_assistant_content(content)
                role = 'OPPONENT'
            else:
                role = 'RESPONDENT'

            # Role label
            set_body(9, 'B')
            if role == 'OPPONENT':
                pdf.set_text_color(100, 80, 160)
            else:
                pdf.set_text_color(60, 100, 80)
            pdf.cell(text=f"{role}:")
            pdf.set_text_color(0, 0, 0)
            pdf.ln(4)

            # Message content
            set_body(10)
            pdf.set_x(pdf.get_x() + 4)
            pdf.multi_cell(w=pdf.w - pdf.get_x() - 20, text=content)
            pdf.ln(4)
    else:
        set_body(10)
        pdf.set_text_color(120, 120, 140)
        pdf.cell(text="(no conversation recorded)")
        pdf.set_text_color(0, 0, 0)

    # ── Output ──

    pdf_bytes = pdf.output()
    logger.info("PDF report generated for '%s': %d bytes, %d pages",
                s['name'], len(pdf_bytes), pdf.pages_count)
    return bytes(pdf_bytes)
