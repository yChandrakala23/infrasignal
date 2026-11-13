"""Builds small synthetic PDFs for tests (label: value lines, optional table). Not real government documents."""
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas


def make_pdf(path, title, lines, pages=None, table=None):
    """lines: list of 'Label: value' strings for page 1. pages: extra pages, each a list of lines. table: optional (header, rows) on its own page."""
    c = canvas.Canvas(str(path), pagesize=A4)
    w, h = A4

    def page(ls, heading):
        y = h - 60
        c.setFont("Helvetica-Bold", 14); c.drawString(50, y, heading); y -= 26
        c.setFont("Helvetica", 11)
        for ln in ls:
            c.drawString(50, y, ln); y -= 18
        c.showPage()
    page(["SAMPLE DOCUMENT - SYNTHETIC, NOT AN OFFICIAL RECORD (test fixture)"] + list(lines), title)
    for extra in pages or []:
        page(extra, title + " (continued)")
    if table:
        header, rows, widths = table
        y = h - 60
        c.setFont("Helvetica-Bold", 12); c.drawString(50, y, title + " - schedule"); y -= 28
        x0 = 40
        def row(vals, bold=False):
            nonlocal y
            c.setFont("Helvetica-Bold" if bold else "Helvetica", 9)
            x = x0
            for v, wd in zip(vals, widths):
                c.rect(x, y - 4, wd, 16)
                c.drawString(x + 3, y, str(v)[: int(wd / 4.6)])
                x += wd
            y -= 16
        row(header, True)
        for r in rows:
            row(r)
        c.showPage()
    c.save()
    return path


def make_boq_pdf(path, pages, header=("Sl No", "Description", "Unit", "Qty", "Rate", "Amount"), widths=(30, 200, 40, 70, 70, 90),
                 title="Bill of Quantities - Test Road", ruled=True, repeat_header=True, two_blocks=False, font=9):
    """Synthetic BOQ. pages: list of pages, each a list of rows (list of strings, one per column).
    repeat_header False -> only the first page carries the column header (a table continued over pages).
    two_blocks True -> each page's rows are split in half and drawn side by side, each half with its own header."""
    c = canvas.Canvas(str(path), pagesize=A4)
    w, h = A4
    cw = font / 2.0 + 0.6  # approx. characters per point of width

    def draw_rows(x0, y, rows, wds):
        for vals, bold in rows:
            c.setFont("Helvetica-Bold" if bold else "Helvetica", font)
            x = x0
            for v, wd in zip(vals, wds):
                if ruled:
                    c.rect(x, y - 4, wd, 16)
                c.drawString(x + 3, y, str(v)[: max(1, int(wd / cw))])
                x += wd
            y -= 16
        return y

    for pi, rows in enumerate(pages):
        c.setFont("Helvetica-Bold", 12)
        c.drawString(40, h - 50, f"{title} - SYNTHETIC TEST FIXTURE, NOT AN OFFICIAL RECORD (page {pi + 1})")
        y0 = h - 80
        show_header = repeat_header or pi == 0
        if two_blocks:
            half = (len(rows) + 1) // 2
            left, right = rows[:half], rows[half:]
            wd2 = list(widths)
            draw_rows(25, y0, ([(list(header), True)] + [(r, False) for r in left]), wd2)
            draw_rows(25 + sum(wd2) + 10, y0, ([(list(header), True)] + [(r, False) for r in right]), wd2)
        else:
            body = [(r, r[1].lower().startswith(("total", "grand", "sub")) if len(r) > 1 else False) for r in rows]
            draw_rows(40, y0, ([(list(header), True)] if show_header else []) + body, list(widths))
        c.showPage()
    c.save()
    return path
